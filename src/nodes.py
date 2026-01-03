import base64
import json
import os
import secrets
import time
from typing import Any, Dict

import httpx
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import interrupt

from utils import (
    AgentState,
    WalletProvider,
    basic_intent_from_task,
    default_service_registry,
    extract_amount_usdc,
    get_header,
    parse_service_identity,
    parse_receipt,
    safe_base64_json,
    score_accepts,
    select_accept,
)
from tools import find_services


def intent_router(state: AgentState) -> AgentState:
    task_text = state.get("task_text")
    updates: Dict[str, Any] = {}
    if task_text:
        llm = state.get("llm_client")
        if not llm:
            updates["intent"] = {"is_payment_task": False, "service_type": "none", "service_query": ""}
            updates["payment_ctx"] = {"status": "failed", "error_type": "llm_required"}
            updates["audit_log"] = [
                {"event": "intent", "error": "llm_required", "timestamp": int(time.time())}
            ]
            return updates
        history = list(state.get("messages", []))
        result, messages = llm.intent(history, task_text, state.get("thread_id"))
        updates["intent"] = result.model_dump()
        updates["messages"] = messages
    else:
        updates["intent"] = {
            "is_payment_task": True,
            "service_type": "direct",
            "service_query": "",
            "query_params": {},
            "constraints": {},
        }
    updates["audit_log"] = [
        {
            "event": "intent",
            "intent": updates.get("intent"),
            "timestamp": int(time.time()),
        }
    ]
    return updates


def service_registry(state: AgentState) -> AgentState:
    if state.get("selected_service"):
        return {}
    updates: Dict[str, Any] = {}
    ctx = state.get("payment_ctx", {})

    if ctx.get("target_url"):
        selected = {
            "name": "direct",
            "url": ctx["target_url"],
            "http_method": ctx.get("http_method", "GET"),
            "supports_x402": True,
            "reputation": "unknown",
            "whitelist_tag": False,
            "headers": ctx.get("request_headers", {}),
            "body": ctx.get("request_body"),
        }
        updates["selected_service"] = selected
        identity = parse_service_identity(selected["url"])
        existing_trust = state.get("risk_flags", {}).get("domain_trusted", False)
        updates["risk_flags"] = {
            **identity,
            "domain_trusted": bool(existing_trust),
            "service_name": selected.get("name", ""),
        }
        updates["payment_ctx"] = {"status": "probing"}
        return updates

    intent = state.get("intent") or {}
    service_query = intent.get("service_query") or ""
    service_type = intent.get("service_type") or ""

    registry = state.get("service_candidates") or find_services(service_query, service_type)
    updates["service_candidates"] = registry
    updates["audit_log"] = [
        {
            "event": "discovery",
            "candidates": registry,
            "timestamp": int(time.time()),
        }
    ]
    selected = None

    llm = state.get("llm_client")
    if not llm:
        return {
            "payment_ctx": {"status": "failed", "error_type": "llm_required"},
            "audit_log": [{"event": "service_select", "error": "llm_required", "timestamp": int(time.time())}],
        }
    history = list(state.get("messages", []))
    payload = {
        "service_query": service_query,
        "service_type": service_type,
        "candidates": registry,
    }
    result, messages = llm.select_service(history, payload, state.get("thread_id"))
    updates["messages"] = messages
    index = result.selected_index
    if isinstance(index, int) and 0 <= index < len(registry):
        selected = registry[index]
    elif result.service_name:
        for svc in registry:
            if str(svc.get("name", "")).lower() == result.service_name.lower():
                selected = svc
                break
    updates["audit_log"] = [
        {
            "event": "service_select",
            "selected_index": result.selected_index,
            "service_name": result.service_name,
            "reason": result.reason,
            "timestamp": int(time.time()),
        }
    ]

    if not selected:
        selected = registry[0] if registry else None

    if not selected:
        return {
            "payment_ctx": {"status": "failed", "error_type": "no_service_candidates"},
            "audit_log": [
                {
                    "event": "service_select",
                    "error": "no_service_candidates",
                    "timestamp": int(time.time()),
                }
            ],
        }

    updates["selected_service"] = selected
    updates["payment_ctx"] = {
        "target_url": selected["url"],
        "http_method": selected.get("http_method", "GET"),
        "request_headers": selected.get("headers", {}),
        "request_body": selected.get("body"),
        "status": "probing",
    }
    identity = parse_service_identity(selected["url"])
    updates["risk_flags"] = {
        **identity,
        "domain_trusted": bool(selected.get("whitelist_tag")),
        "service_name": selected.get("name", ""),
    }
    return updates


def request_executor(state: AgentState) -> AgentState:
    ctx = state.get("payment_ctx", {})
    headers = dict(ctx.get("request_headers", {}))
    updates: Dict[str, Any] = {}
    if ctx.get("signed_payload"):
        headers["PAYMENT-SIGNATURE"] = ctx["signed_payload"]
        updates["payment_ctx"] = {"status": "submitted"}
    if ctx.get("target_url"):
        updates["risk_flags"] = parse_service_identity(ctx["target_url"])

    client = state.get("http_client")
    try:
        if client:
            response = client.request(
                ctx.get("http_method", "GET"),
                ctx["target_url"],
                headers=headers,
                json=ctx.get("request_body"),
                timeout=10.0,
            )
        else:
            response = httpx.request(
                ctx.get("http_method", "GET"),
                ctx["target_url"],
                headers=headers,
                json=ctx.get("request_body"),
                timeout=10.0,
            )
    except httpx.RequestError as exc:
        updates["payment_ctx"] = {
            "status": "failed",
            "error_type": "network_error",
            "error_msg": str(exc),
        }
        return updates

    updates.setdefault("payment_ctx", {})
    updates["payment_ctx"].update(
        {
            "last_http_status": response.status_code,
            "last_response_headers": dict(response.headers),
            "last_response_text": response.text,
        }
    )

    if response.status_code == 200:
        updates["payment_ctx"].update(
            {"status": "success", "error_type": None, "error_msg": None}
        )
    elif response.status_code == 402:
        headers = dict(response.headers)
        required = get_header(headers, "PAYMENT-REQUIRED") or get_header(headers, "X-PAYMENT-REQUIRED")
        updates["payment_ctx"].update(
            {"status": "payment_required", "requirements_raw": required}
        )
        if ctx.get("signed_payload"):
            retry_count = int(ctx.get("retry_count", 0)) + 1
            updates["payment_ctx"]["retry_count"] = retry_count
            if retry_count >= int(ctx.get("max_payment_retries", 3)):
                updates["payment_ctx"].update(
                    {"status": "failed", "error_type": "payment_loop"}
                )
            else:
                updates["payment_ctx"]["error_type"] = "payment_verification_failed"
        else:
            updates["payment_ctx"]["error_type"] = None
    elif 500 <= response.status_code < 600:
        updates["payment_ctx"].update(
            {"status": "failed", "error_type": "http_server_error"}
        )
    elif 400 <= response.status_code < 500:
        updates["payment_ctx"].update(
            {"status": "failed", "error_type": "http_client_error"}
        )
    else:
        updates["payment_ctx"].update(
            {"status": "failed", "error_type": "http_unknown"}
        )

    return updates


def header_parser(state: AgentState) -> AgentState:
    ctx = state.get("payment_ctx", {})
    raw = ctx.get("requirements_raw")
    if not raw:
        return {"payment_ctx": {"status": "failed", "error_type": "missing_payment_required"}}

    try:
        requirements = safe_base64_json(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        return {
            "payment_ctx": {
                "status": "failed",
                "error_type": "bad_payment_required",
                "error_msg": str(exc),
            }
        }

    accepts = requirements.get("accepts", [])
    if not accepts:
        return {"payment_ctx": {"status": "failed", "error_type": "no_accepts"}}
    return {"payment_ctx": {"requirements": requirements, "status": "analyzing"}}


def payment_negotiator(state: AgentState) -> AgentState:
    ctx = state.get("payment_ctx", {})
    updates: Dict[str, Any] = {}
    requirements = ctx.get("requirements") or {}
    llm = state.get("llm_client")
    if not llm:
        return {
            "payment_ctx": {"status": "failed", "error_type": "llm_required"},
            "audit_log": [{"event": "negotiation", "error": "llm_required", "timestamp": int(time.time())}],
        }
    selected = None
    accepts = requirements.get("accepts", [])
    payload = {
        "accepts": accepts,
        "preferences": state.get("payment_preferences", {}),
        "wallet_balance": state.get("wallet_balance", 0.0),
        "risk_flags": state.get("risk_flags", {}),
    }
    history = list(state.get("messages", []))
    os.environ["MOCK_WALLET_BALANCE"] = str(state.get("wallet_balance", 0.0))
    result, messages = llm.negotiate(history, payload, state.get("thread_id"))
    index = result.selected_index
    if isinstance(index, int) and 0 <= index < len(accepts):
        selected = accepts[index]
        updates["policy_decision"] = result.policy_decision
        updates["negotiation_result"] = {
            "selected_index": index,
            "reason": result.reason,
        }
    updates["messages"] = messages
    if selected is None:
        return {
            "payment_ctx": {"status": "failed", "error_type": "no_accepts"},
            "audit_log": [{"event": "negotiation", "error": "no_accepts", "timestamp": int(time.time())}],
        }
    if not selected:
        updates["payment_ctx"] = {"status": "failed", "error_type": "no_accepts"}
        return updates
    scheme = (selected.get("scheme") or "exact").lower()
    if scheme not in {"exact"}:
        updates["payment_ctx"] = {"status": "failed", "error_type": "unsupported_scheme"}
        return updates
    if not selected.get("network") or not selected.get("asset"):
        updates["payment_ctx"] = {"status": "failed", "error_type": "invalid_accept"}
        return updates
    amount_usdc = extract_amount_usdc(selected, requirements)
    if amount_usdc <= 0:
        updates["payment_ctx"] = {"status": "failed", "error_type": "invalid_amount"}
        return updates
    updates["negotiation_result"] = {
        "selected": selected,
        "reason": "preference_match",
        "amount_usdc": amount_usdc,
    }
    if not state.get("policy_decision") and "policy_decision" not in updates:
        updates["policy_decision"] = "pending"
    updates["audit_log"] = [
        {
            "event": "negotiation",
            "result": updates.get("negotiation_result") or state.get("negotiation_result"),
            "policy_decision": updates.get("policy_decision") or state.get("policy_decision"),
            "timestamp": int(time.time()),
        }
    ]
    updates["payment_ctx"] = {
        "selected_accept": selected,
        "amount_usdc": amount_usdc,
        "status": "negotiating",
    }
    return updates


def risk_assessor(state: AgentState) -> AgentState:
    ctx = state.get("payment_ctx", {})
    updates: Dict[str, Any] = {}
    amount = ctx.get("amount_usdc", 0.0)
    risk_flags = state.get("risk_flags", {})
    domain_trusted = risk_flags.get("domain_trusted", True)
    payee_reputation = risk_flags.get("payee_reputation", "unknown")
    policy_blocked = risk_flags.get("policy_blocked", False)
    force_reject = risk_flags.get("force_reject", False)
    policy = state.get("policy", {})
    auto_pay_threshold = float(policy.get("auto_pay_threshold", state["auto_approve_threshold"]))
    hitl_threshold = float(policy.get("hitl_threshold", auto_pay_threshold))
    hard_limit = float(policy.get("hard_limit", state["budget_limit"]))
    whitelist_domains = {d.lower() for d in policy.get("whitelist_domains", [])}
    whitelist_services = {s.lower() for s in policy.get("whitelist_services", [])}
    payee_blacklist = {p.lower() for p in policy.get("payee_blacklist", [])}
    service_domain = str(risk_flags.get("service_domain", "")).lower()
    service_name = str(risk_flags.get("service_name", "")).lower()
    risk_reasons = []
    risk_score = 0.0

    if state["wallet_balance"] < amount:
        updates["policy_decision"] = "reject"
        updates["risk_reasons"] = ["insufficient_balance"]
        updates["risk_score"] = 3.0
        updates["payment_ctx"] = {"status": "failed", "error_type": "insufficient_balance"}
        return updates

    if policy_blocked or force_reject:
        updates["policy_decision"] = "reject"
        updates["risk_reasons"] = ["policy_reject"]
        updates["risk_score"] = 3.0
        updates["payment_ctx"] = {"status": "failed", "error_type": "policy_reject"}
        return updates

    if amount > hard_limit:
        updates["policy_decision"] = "reject"
        updates["risk_reasons"] = ["hard_limit_exceeded"]
        updates["risk_score"] = 2.5
        updates["payment_ctx"] = {"status": "failed", "error_type": "policy_reject"}
        return updates

    payee = (ctx.get("selected_accept") or {}).get("to", "")
    if payee and payee.lower() in payee_blacklist:
        updates["policy_decision"] = "reject"
        risk_reasons.append("payee_blacklisted")
        risk_score += 3.0
        updates["risk_reasons"] = risk_reasons
        updates["risk_score"] = risk_score
        updates["payment_ctx"] = {"status": "failed", "error_type": "policy_reject"}
        return updates

    if whitelist_domains and service_domain in whitelist_domains:
        domain_trusted = True
    if whitelist_services and service_name in whitelist_services:
        domain_trusted = True

    if not domain_trusted or payee_reputation == "bad":
        updates["human_approval_required"] = True
        updates["policy_decision"] = "hitl"
        risk_reasons.append("untrusted_service")
        risk_score += 1.5
        updates["risk_reasons"] = risk_reasons
        updates["risk_score"] = risk_score
        updates["payment_ctx"] = {"status": "risk_check"}
        return updates

    if state["session_spend"] + amount > state["budget_limit"]:
        updates["human_approval_required"] = True
        updates["policy_decision"] = "hitl"
        risk_reasons.append("budget_exceeded")
        risk_score += 1.0
        updates["risk_reasons"] = risk_reasons
        updates["risk_score"] = risk_score
        updates["payment_ctx"] = {"status": "risk_check"}
        return updates

    if amount <= auto_pay_threshold and amount <= hitl_threshold:
        updates["human_approval_required"] = False
        updates["policy_decision"] = "auto"
    else:
        updates["human_approval_required"] = True
        updates["policy_decision"] = "hitl"
        risk_reasons.append("amount_over_threshold")
        risk_score += 1.0

    updates["risk_reasons"] = risk_reasons
    updates["risk_score"] = risk_score
    updates["audit_log"] = [
        {
            "event": "policy",
            "decision": updates.get("policy_decision"),
            "risk_score": risk_score,
            "risk_reasons": risk_reasons,
            "timestamp": int(time.time()),
        }
    ]
    updates["payment_ctx"] = {"status": "risk_check"}
    return updates


def reflection_rewriter(state: AgentState) -> AgentState:
    ctx = state.get("payment_ctx", {})
    updates: Dict[str, Any] = {}
    error_type = ctx.get("error_type")
    llm = state.get("llm_client")
    if not llm:
        return {
            "payment_ctx": {"status": "failed", "error_type": "llm_required"},
            "audit_log": [{"event": "reflection", "error": "llm_required", "timestamp": int(time.time())}],
        }
    history = list(state.get("messages", []))
    payload = {
        "error_type": error_type,
        "retry_count": ctx.get("retry_count"),
        "last_http_status": ctx.get("last_http_status"),
    }
    result, messages = llm.reflect(
        history,
        {
            "error_type": error_type,
            "retry_count": ctx.get("retry_count"),
            "last_http_status": ctx.get("last_http_status"),
        },
        state.get("thread_id"),
    )
    notes = result.model_dump()
    updates["messages"] = messages
    if notes.get("action") == "reselect_accept":
        updates["payment_ctx"] = {"selected_accept": None}
    if notes.get("action") == "retry_request":
        updates["payment_ctx"] = {"status": "retrying"}
    blacklist_payee = notes.get("blacklist_payee")
    if blacklist_payee:
        policy = dict(state.get("policy", {}))
        policy.setdefault("payee_blacklist", [])
        if blacklist_payee not in policy["payee_blacklist"]:
            policy["payee_blacklist"].append(blacklist_payee)
        updates["policy"] = policy
    if notes.get("force_human") is True:
        updates["human_approval_required"] = True
        updates["policy_decision"] = "hitl"
    switch_network = notes.get("switch_network")
    if switch_network:
        prefs = dict(state.get("payment_preferences", {}))
        prefs["network"] = str(switch_network).lower()
        updates["payment_preferences"] = prefs
    switch_asset = notes.get("switch_asset")
    if switch_asset:
        prefs = dict(state.get("payment_preferences", {}))
        prefs["asset"] = str(switch_asset).lower()
        updates["payment_preferences"] = prefs
    updates["reflection_notes"] = notes
    updates["audit_log"] = [
        {
            "event": "reflection",
            "notes": notes,
            "timestamp": int(time.time()),
        }
    ]
    return updates


def human_approver(state: AgentState) -> AgentState:
    ctx = state["payment_ctx"]
    prompt = {
        "amount_usdc": ctx.get("amount_usdc"),
        "selected_accept": ctx.get("selected_accept"),
        "reason": "Payment requires approval",
    }
    decision = interrupt(prompt)
    updates: Dict[str, Any] = {
        "user_decision": decision,
        "audit_log": [
            {
                "event": "hitl",
                "decision": decision,
                "timestamp": int(time.time()),
            }
        ],
    }
    if decision == "reject":
        updates["payment_ctx"] = {"status": "failed", "error_type": "user_reject"}
    return updates


def payment_signer(state: AgentState, wallet_provider: WalletProvider) -> AgentState:
    ctx = state.get("payment_ctx", {})
    selected = ctx.get("selected_accept")
    if not selected:
        return {"payment_ctx": {"status": "failed", "error_type": "no_accept_selected"}}

    to_address = selected.get("to") or selected.get("payee") or selected.get("recipient")
    if not to_address:
        return {"payment_ctx": {"status": "failed", "error_type": "missing_payee"}}

    amount = ctx.get("amount_usdc", 0.0)
    decimals = selected.get("decimals", 6)
    value = int(amount * (10 ** int(decimals)))

    domain: Dict[str, Any] = {
        "name": selected.get("token_name", "USD Coin"),
        "version": selected.get("token_version", "2"),
        "chainId": selected.get("chainId", 8453),
        "verifyingContract": selected.get("token_address", "0x0000000000000000000000000000000000000000"),
    }
    types = {
        "EIP712Domain": [
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
        ],
        "TransferWithAuthorization": [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "validAfter", "type": "uint256"},
            {"name": "validBefore", "type": "uint256"},
            {"name": "nonce", "type": "bytes32"},
        ],
    }
    authorization = {
        "from": wallet_provider.get_address(),
        "to": to_address,
        "value": str(value),
        "validAfter": 0,
        "validBefore": int(time.time()) + 3600,
        "nonce": "0x" + secrets.token_hex(32),
    }

    signature = wallet_provider.sign_typed_data(domain, types, authorization)
    payload = {
        "x402Version": "1.0",
        "scheme": "exact",
        "network": selected.get("network"),
        "payload": {
            "signature": signature,
            "authorization": authorization,
            "domain": domain,
            "types": types,
            "asset": selected.get("asset"),
        },
    }

    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
    return {
        "payment_ctx": {"signed_payload": encoded, "status": "authorized"},
        "session_spend": state.get("session_spend", 0.0) + amount,
    }


def response_validator(state: AgentState) -> AgentState:
    ctx = state.get("payment_ctx", {})
    headers = ctx.get("last_response_headers") or {}
    receipt_raw = get_header(headers, "PAYMENT-RESPONSE") or get_header(headers, "X-PAYMENT-RESPONSE")
    tx_hash = None
    if receipt_raw:
        receipt = parse_receipt(receipt_raw)
        tx_hash = receipt.get("txHash") or receipt.get("transactionHash") or receipt.get("hash") or receipt.get("raw")

    return {
        "payment_ctx": {"tx_hash": tx_hash, "status": "success"},
        "audit_log": [
            {
                "amount_usdc": ctx.get("amount_usdc"),
                "payee": (ctx.get("selected_accept") or {}).get("to"),
                "network": (ctx.get("selected_accept") or {}).get("network"),
                "tx_hash": tx_hash,
                "timestamp": int(time.time()),
            }
        ],
    }


def error_handler(state: AgentState) -> AgentState:
    ctx = state.get("payment_ctx", {})
    error_type = ctx.get("error_type")
    updates: Dict[str, Any] = {}
    if error_type in {"network_error", "http_server_error"}:
        if ctx.get("network_retry_count", 0) < ctx.get("max_network_retries", 3):
            retry_count = int(ctx.get("network_retry_count", 0)) + 1
            updates["payment_ctx"] = {"network_retry_count": retry_count}
            time.sleep(min(2 ** retry_count, 8))
    if error_type in {"payment_verification_failed", "payment_loop"}:
        updates.setdefault("payment_ctx", {})
        updates["payment_ctx"].update({"signed_payload": None, "status": "failed"})
    if error_type in {"insufficient_balance", "policy_reject", "user_reject"}:
        updates.setdefault("payment_ctx", {})
        updates["payment_ctx"].update({"status": "failed"})
    return updates


def normal_chat(state: AgentState) -> AgentState:
    task_text = state.get("task_text") or ""
    return {
        "messages": [AIMessage(content=f"No payment required for task: {task_text}")],
        "audit_log": [
            {
                "event": "normal_chat",
                "timestamp": int(time.time()),
            }
        ],
    }
