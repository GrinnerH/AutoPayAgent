import base64
import json
import secrets
import time
from typing import Any, Dict

import httpx
from langchain.messages import HumanMessage
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


def intent_router(state: AgentState) -> AgentState:
    task_text = state.get("task_text")
    updates: Dict[str, Any] = {}
    if task_text:
        llm = state.get("llm_client")
        if llm:
            history = list(state.get("messages", []))
            result, messages = llm.intent(history, task_text, state.get("thread_id"))
            updates["intent"] = result.model_dump()
            updates["messages"] = messages
        else:
            updates["intent"] = basic_intent_from_task(task_text)
    else:
        updates["intent"] = {
            "is_payment_task": True,
            "service_type": "direct",
            "query_params": {},
            "constraints": {},
        }
    return updates


def service_registry(state: AgentState) -> AgentState:
    if state.get("selected_service"):
        return {}
    updates: Dict[str, Any] = {}
    registry = state.get("service_candidates") or default_service_registry()
    updates["service_candidates"] = registry
    if state["payment_ctx"].get("target_url"):
        updates["selected_service"] = {
            "name": "direct",
            "url": state["payment_ctx"]["target_url"],
            "http_method": state["payment_ctx"].get("http_method", "GET"),
            "supports_x402": True,
            "reputation": "unknown",
            "whitelist_tag": False,
            "headers": state["payment_ctx"].get("request_headers", {}),
            "body": state["payment_ctx"].get("request_body"),
        }
        return updates

    selected = registry[0] if registry else None
    updates["selected_service"] = selected
    if selected:
        ctx = dict(state["payment_ctx"])
        ctx["target_url"] = selected["url"]
        ctx["http_method"] = selected.get("http_method", "GET")
        ctx["request_headers"] = selected.get("headers", {})
        ctx["request_body"] = selected.get("body")
        identity = parse_service_identity(selected["url"])
        risk_flags = dict(state.get("risk_flags", {}))
        risk_flags.update(identity)
        risk_flags["domain_trusted"] = bool(selected.get("whitelist_tag"))
        risk_flags["service_name"] = selected.get("name", "")
        updates["payment_ctx"] = ctx
        updates["risk_flags"] = risk_flags
    return updates


def request_executor(state: AgentState) -> AgentState:
    ctx = dict(state["payment_ctx"])
    headers = dict(ctx.get("request_headers", {}))
    if ctx.get("signed_payload"):
        headers["PAYMENT-SIGNATURE"] = ctx["signed_payload"]
        ctx["status"] = "submitted"
    if ctx.get("target_url"):
        risk_flags = dict(state.get("risk_flags", {}))
        risk_flags.update(parse_service_identity(ctx["target_url"]))
    else:
        risk_flags = state.get("risk_flags", {})

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
        ctx["status"] = "failed"
        ctx["error_type"] = "network_error"
        ctx["error_msg"] = str(exc)
        return {"payment_ctx": ctx, "risk_flags": risk_flags}

    ctx["last_http_status"] = response.status_code
    ctx["last_response_headers"] = dict(response.headers)
    ctx["last_response_text"] = response.text

    if response.status_code == 200:
        ctx["status"] = "success"
        ctx["error_type"] = None
        ctx["error_msg"] = None
    elif response.status_code == 402:
        ctx["status"] = "payment_required"
        headers = dict(response.headers)
        ctx["requirements_raw"] = get_header(headers, "PAYMENT-REQUIRED") or get_header(headers, "X-PAYMENT-REQUIRED")
        if ctx.get("signed_payload"):
            ctx["retry_count"] += 1
            if ctx["retry_count"] >= ctx["max_payment_retries"]:
                ctx["status"] = "failed"
                ctx["error_type"] = "payment_loop"
            else:
                ctx["error_type"] = "payment_verification_failed"
        else:
            ctx["error_type"] = None
    elif 500 <= response.status_code < 600:
        ctx["status"] = "failed"
        ctx["error_type"] = "http_server_error"
    elif 400 <= response.status_code < 500:
        ctx["status"] = "failed"
        ctx["error_type"] = "http_client_error"
    else:
        ctx["status"] = "failed"
        ctx["error_type"] = "http_unknown"

    return {"payment_ctx": ctx, "risk_flags": risk_flags}


def header_parser(state: AgentState) -> AgentState:
    ctx = dict(state["payment_ctx"])
    raw = ctx.get("requirements_raw")
    if not raw:
        ctx["status"] = "failed"
        ctx["error_type"] = "missing_payment_required"
        return {"payment_ctx": ctx}

    try:
        requirements = safe_base64_json(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        ctx["status"] = "failed"
        ctx["error_type"] = "bad_payment_required"
        ctx["error_msg"] = str(exc)
        return {"payment_ctx": ctx}

    ctx["requirements"] = requirements
    accepts = requirements.get("accepts", [])
    if not accepts:
        ctx["status"] = "failed"
        ctx["error_type"] = "no_accepts"
        return {"payment_ctx": ctx}
    ctx["status"] = "analyzing"
    return {"payment_ctx": ctx}


def payment_negotiator(state: AgentState) -> AgentState:
    ctx = dict(state["payment_ctx"])
    updates: Dict[str, Any] = {}
    requirements = ctx.get("requirements") or {}
    llm = state.get("llm_client")
    selected = None
    if llm:
        accepts = requirements.get("accepts", [])
        payload = {
            "accepts": accepts,
            "preferences": state.get("payment_preferences", {}),
            "wallet_balance": state.get("wallet_balance", 0.0),
            "risk_flags": state.get("risk_flags", {}),
        }
        history = list(state.get("messages", []))
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
        accepts = requirements.get("accepts", [])
        ranked = score_accepts(accepts, state.get("payment_preferences", {})) if accepts else []
        if ranked:
            selected = accepts[ranked[0]]
        else:
            selected = select_accept(requirements, state.get("payment_preferences", {}))
    if not selected:
        ctx["status"] = "failed"
        ctx["error_type"] = "no_accepts"
        updates["payment_ctx"] = ctx
        return updates
    scheme = (selected.get("scheme") or "exact").lower()
    if scheme not in {"exact"}:
        ctx["status"] = "failed"
        ctx["error_type"] = "unsupported_scheme"
        updates["payment_ctx"] = ctx
        return updates
    if not selected.get("network") or not selected.get("asset"):
        ctx["status"] = "failed"
        ctx["error_type"] = "invalid_accept"
        updates["payment_ctx"] = ctx
        return updates
    ctx["selected_accept"] = selected
    ctx["amount_usdc"] = extract_amount_usdc(selected, requirements)
    if ctx["amount_usdc"] <= 0:
        ctx["status"] = "failed"
        ctx["error_type"] = "invalid_amount"
        updates["payment_ctx"] = ctx
        return updates
    updates["negotiation_result"] = {
        "selected": selected,
        "reason": "preference_match",
        "amount_usdc": ctx["amount_usdc"],
    }
    if not state.get("policy_decision") and "policy_decision" not in updates:
        updates["policy_decision"] = "pending"
    audit_log = list(state.get("audit_log", []))
    audit_log.append(
        {
            "event": "negotiation",
            "result": updates.get("negotiation_result") or state.get("negotiation_result"),
            "policy_decision": updates.get("policy_decision") or state.get("policy_decision"),
            "timestamp": int(time.time()),
        }
    )
    updates["payment_ctx"] = ctx
    updates["audit_log"] = audit_log
    return updates


def risk_assessor(state: AgentState) -> AgentState:
    ctx = dict(state["payment_ctx"])
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
        ctx["status"] = "failed"
        ctx["error_type"] = "insufficient_balance"
        updates["policy_decision"] = "reject"
        updates["risk_reasons"] = ["insufficient_balance"]
        updates["risk_score"] = 3.0
        updates["payment_ctx"] = ctx
        return updates

    if policy_blocked or force_reject:
        ctx["status"] = "failed"
        ctx["error_type"] = "policy_reject"
        updates["policy_decision"] = "reject"
        updates["risk_reasons"] = ["policy_reject"]
        updates["risk_score"] = 3.0
        updates["payment_ctx"] = ctx
        return updates

    if amount > hard_limit:
        ctx["status"] = "failed"
        ctx["error_type"] = "policy_reject"
        updates["policy_decision"] = "reject"
        updates["risk_reasons"] = ["hard_limit_exceeded"]
        updates["risk_score"] = 2.5
        updates["payment_ctx"] = ctx
        return updates

    payee = (ctx.get("selected_accept") or {}).get("to", "")
    if payee and payee.lower() in payee_blacklist:
        ctx["status"] = "failed"
        ctx["error_type"] = "policy_reject"
        updates["policy_decision"] = "reject"
        risk_reasons.append("payee_blacklisted")
        risk_score += 3.0
        updates["risk_reasons"] = risk_reasons
        updates["risk_score"] = risk_score
        updates["payment_ctx"] = ctx
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
        updates["payment_ctx"] = ctx
        return updates

    if state["session_spend"] + amount > state["budget_limit"]:
        updates["human_approval_required"] = True
        updates["policy_decision"] = "hitl"
        risk_reasons.append("budget_exceeded")
        risk_score += 1.0
        updates["risk_reasons"] = risk_reasons
        updates["risk_score"] = risk_score
        updates["payment_ctx"] = ctx
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
    audit_log = list(state.get("audit_log", []))
    audit_log.append(
        {
            "event": "policy",
            "decision": updates.get("policy_decision"),
            "risk_score": risk_score,
            "risk_reasons": risk_reasons,
            "timestamp": int(time.time()),
        }
    )

    updates["audit_log"] = audit_log
    updates["payment_ctx"] = ctx
    return updates


def reflection_rewriter(state: AgentState) -> AgentState:
    ctx = dict(state["payment_ctx"])
    updates: Dict[str, Any] = {}
    error_type = ctx.get("error_type")
    llm = state.get("llm_client")
    if llm:
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
    else:
        if error_type in {"payment_verification_failed", "payment_loop"}:
            notes = {"action": "reselect_accept", "reason": error_type}
        elif error_type in {"network_error", "http_server_error"}:
            notes = {"action": "retry_request", "reason": error_type}
        else:
            notes = {"action": "abort", "reason": error_type}
    state["reflection_notes"] = notes
    if notes.get("action") == "reselect_accept":
        ctx["selected_accept"] = None
    if notes.get("action") == "retry_request":
        pass
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
    audit_log = list(state.get("audit_log", []))
    audit_log.append(
        {
            "event": "reflection",
            "notes": notes,
            "timestamp": int(time.time()),
        }
    )
    updates["payment_ctx"] = ctx
    updates["reflection_notes"] = notes
    updates["audit_log"] = audit_log
    return updates


def human_approver(state: AgentState) -> AgentState:
    ctx = state["payment_ctx"]
    prompt = {
        "amount_usdc": ctx.get("amount_usdc"),
        "selected_accept": ctx.get("selected_accept"),
        "reason": "Payment requires approval",
    }
    decision = interrupt(prompt)
    return {"user_decision": decision}


def payment_signer(state: AgentState, wallet_provider: WalletProvider) -> AgentState:
    ctx = dict(state["payment_ctx"])
    selected = ctx.get("selected_accept")
    if not selected:
        ctx["status"] = "failed"
        ctx["error_type"] = "no_accept_selected"
        return {"payment_ctx": ctx}

    to_address = selected.get("to") or selected.get("payee") or selected.get("recipient")
    if not to_address:
        ctx["status"] = "failed"
        ctx["error_type"] = "missing_payee"
        return {"payment_ctx": ctx}

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
        "TransferWithAuthorization": [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "validAfter", "type": "uint256"},
            {"name": "validBefore", "type": "uint256"},
            {"name": "nonce", "type": "bytes32"},
        ]
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
        "payload": {"signature": signature, "authorization": authorization},
    }

    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
    ctx["signed_payload"] = encoded
    ctx["status"] = "authorized"
    return {
        "payment_ctx": ctx,
        "session_spend": state.get("session_spend", 0.0) + amount,
    }


def response_validator(state: AgentState) -> AgentState:
    ctx = dict(state["payment_ctx"])
    headers = ctx.get("last_response_headers") or {}
    receipt_raw = get_header(headers, "PAYMENT-RESPONSE") or get_header(headers, "X-PAYMENT-RESPONSE")
    tx_hash = None
    if receipt_raw:
        receipt = parse_receipt(receipt_raw)
        tx_hash = receipt.get("txHash") or receipt.get("transactionHash") or receipt.get("hash") or receipt.get("raw")

    ctx["tx_hash"] = tx_hash
    ctx["status"] = "success"
    audit_log = list(state.get("audit_log", []))
    audit_log.append(
        {
            "amount_usdc": ctx.get("amount_usdc"),
            "payee": (ctx.get("selected_accept") or {}).get("to"),
            "network": (ctx.get("selected_accept") or {}).get("network"),
            "tx_hash": tx_hash,
            "timestamp": int(time.time()),
        }
    )
    return {"payment_ctx": ctx, "audit_log": audit_log}


def error_handler(state: AgentState) -> AgentState:
    ctx = dict(state["payment_ctx"])
    error_type = ctx.get("error_type")
    if error_type in {"network_error", "http_server_error"}:
        if ctx["network_retry_count"] < ctx["max_network_retries"]:
            ctx["network_retry_count"] += 1
            time.sleep(min(2 ** ctx["network_retry_count"], 8))
    if error_type in {"payment_verification_failed", "payment_loop"}:
        ctx["signed_payload"] = None
        ctx["status"] = "failed"
    if error_type in {"insufficient_balance", "policy_reject", "user_reject"}:
        ctx["status"] = "failed"
    return {"payment_ctx": ctx}
