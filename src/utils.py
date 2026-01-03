import base64
import json
import operator
from dataclasses import dataclass
from typing import Annotated, Any, Dict, List, Optional, TypedDict
from urllib.parse import urlparse

from langchain_core.messages import AnyMessage


def merge_dict_shallow(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(old or {})
    merged.update(new or {})
    return merged

class PaymentContext(TypedDict):
    target_url: str
    http_method: str
    request_headers: Dict[str, str]
    request_body: Optional[Any]
    status: str
    requirements: Optional[Dict[str, Any]]
    requirements_raw: Optional[str]
    selected_accept: Optional[Dict[str, Any]]
    signed_payload: Optional[str]
    amount_usdc: float
    tx_hash: Optional[str]
    last_http_status: Optional[int]
    last_response_headers: Optional[Dict[str, str]]
    last_response_text: Optional[str]
    error_type: Optional[str]
    error_msg: Optional[str]
    retry_count: int
    network_retry_count: int
    max_payment_retries: int
    max_network_retries: int


class AgentState(TypedDict):
    messages: Annotated[List[AnyMessage], operator.add]
    thread_id: Optional[str]
    payment_ctx: Annotated[PaymentContext, merge_dict_shallow]
    http_client: Optional[Any]
    payment_preferences: Annotated[Dict[str, Any], merge_dict_shallow]
    task_text: Optional[str]
    intent: Optional[Dict[str, Any]]
    service_candidates: List[Dict[str, Any]]
    selected_service: Optional[Dict[str, Any]]
    negotiation_result: Optional[Dict[str, Any]]
    policy_decision: Optional[str]
    reflection_notes: Optional[Dict[str, Any]]
    risk_score: Optional[float]
    risk_reasons: Annotated[List[str], operator.add]
    llm_client: Optional[Any]
    policy: Annotated[Dict[str, Any], merge_dict_shallow]
    wallet_balance: float
    session_spend: float
    budget_limit: float
    auto_approve_threshold: float
    human_approval_required: bool
    user_decision: Optional[str]
    risk_flags: Annotated[Dict[str, Any], merge_dict_shallow]
    audit_log: Annotated[List[Dict[str, Any]], operator.add]


@dataclass
class WalletProvider:
    def get_address(self) -> str:
        raise NotImplementedError

    def sign_typed_data(self, domain: Dict[str, Any], types: Dict[str, Any], message: Dict[str, Any]) -> str:
        raise NotImplementedError


@dataclass
class DummyWalletProvider(WalletProvider):
    address: str = "0x0000000000000000000000000000000000000000"

    def get_address(self) -> str:
        return self.address

    def sign_typed_data(self, domain: Dict[str, Any], types: Dict[str, Any], message: Dict[str, Any]) -> str:
        payload = json.dumps({"domain": domain, "types": types, "message": message}, sort_keys=True)
        return "0x" + payload.encode("utf-8").hex()[:130]


@dataclass
class AgentKitWalletProvider(WalletProvider):
    provider: Any

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "AgentKitWalletProvider":
        try:
            from coinbase_agentkit import CdpWalletProvider, CdpWalletProviderConfig
        except ImportError as exc:
            raise RuntimeError("coinbase_agentkit is not installed") from exc

        provider = CdpWalletProvider(
            CdpWalletProviderConfig(
                api_key_name=config["api_key_name"],
                api_key_secret=config["api_key_secret"],
                network_id=config.get("network_id", "base-mainnet"),
            )
        )
        return cls(provider=provider)

    def get_address(self) -> str:
        return self.provider.get_address()

    def sign_typed_data(self, domain: Dict[str, Any], types: Dict[str, Any], message: Dict[str, Any]) -> str:
        if hasattr(self.provider, "sign_typed_data"):
            return self.provider.sign_typed_data(domain, types, message)
        if hasattr(self.provider, "sign_typed_data_v4"):
            return self.provider.sign_typed_data_v4(domain, types, message)
        raise RuntimeError("Wallet provider does not support typed data signing")


@dataclass
class EthAccountWalletProvider(WalletProvider):
    private_key: str

    @classmethod
    def from_env(cls) -> "EthAccountWalletProvider":
        import os
        key = os.getenv("ETH_PRIVATE_KEY", "")
        if not key:
            from eth_account import Account
            key = Account.create().key.hex()
        return cls(private_key=key)

    def get_address(self) -> str:
        from eth_account import Account
        return Account.from_key(self.private_key).address

    def sign_typed_data(self, domain: Dict[str, Any], types: Dict[str, Any], message: Dict[str, Any]) -> str:
        from eth_account import Account
        from eth_account.messages import encode_structured_data
        data = {
            "types": types,
            "primaryType": "TransferWithAuthorization",
            "domain": domain,
            "message": message,
        }
        signable = encode_structured_data(data)
        signed = Account.sign_message(signable, private_key=self.private_key)
        return signed.signature.hex()


def safe_base64_json(raw: str) -> Dict[str, Any]:
    padded = raw + "=" * (-len(raw) % 4)
    decoded = base64.b64decode(padded).decode("utf-8")
    return json.loads(decoded)




def extract_amount_usdc(accept: Dict[str, Any], requirements: Dict[str, Any]) -> float:
    amount = accept.get("amount") or accept.get("maxAmountRequired") or requirements.get("maxAmountRequired")
    if amount is None:
        return 0.0
    decimals = accept.get("decimals")
    asset = accept.get("asset") or requirements.get("asset") or {}
    if decimals is None:
        decimals = asset.get("decimals", 6)
    try:
        return float(amount) / (10 ** int(decimals))
    except (ValueError, TypeError):
        try:
            return float(amount)
        except (ValueError, TypeError):
            return 0.0


def get_header(headers: Dict[str, str], name: str) -> Optional[str]:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def parse_receipt(raw: str) -> Dict[str, Any]:
    try:
        return safe_base64_json(raw)
    except (json.JSONDecodeError, ValueError):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {"raw": raw}


def select_accept(requirements: Dict[str, Any], preferences: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    accepts = requirements.get("accepts", [])
    if not accepts:
        return None

    pref_network = str(preferences.get("network", "")).lower()
    pref_asset = str(preferences.get("asset", "")).lower()
    pref_scheme = str(preferences.get("scheme", "")).lower()

    def score(candidate: Dict[str, Any]) -> tuple:
        network = str(candidate.get("network", "")).lower()
        asset = str(candidate.get("asset", "")).lower()
        scheme = str(candidate.get("scheme", preferences.get("scheme", "exact"))).lower()
        amount = extract_amount_usdc(candidate, requirements)
        matches = 0
        if pref_network and network == pref_network:
            matches += 1
        if pref_asset and pref_asset in asset:
            matches += 1
        if pref_scheme and scheme == pref_scheme:
            matches += 1
        return (-matches, amount)

    return sorted(accepts, key=score)[0]


def score_accepts(accepts: List[Dict[str, object]], preferences: Dict[str, object]) -> List[int]:
    """Score accepts by preference match and amount (lower is better)."""
    pref_network = str(preferences.get("network", "")).lower()
    pref_asset = str(preferences.get("asset", "")).lower()
    pref_scheme = str(preferences.get("scheme", "")).lower()

    def score(candidate: Dict[str, object]) -> tuple:
        network = str(candidate.get("network", "")).lower()
        asset = str(candidate.get("asset", "")).lower()
        scheme = str(candidate.get("scheme", preferences.get("scheme", "exact"))).lower()
        amount = extract_amount_usdc(candidate, {"accepts": accepts})
        matches = 0
        if pref_network and network == pref_network:
            matches += 1
        if pref_asset and pref_asset in asset:
            matches += 1
        if pref_scheme and scheme == pref_scheme:
            matches += 1
        return (-matches, amount)

    ranked = sorted(range(len(accepts)), key=lambda idx: score(accepts[idx]))
    return ranked


def basic_intent_from_task(task_text: str) -> Dict[str, Any]:
    text = task_text.lower()
    if any(term in text for term in ["闲聊", "普通问题", "聊天", "general chat", "small talk"]):
        return {
            "is_payment_task": False,
            "service_type": "none",
            "service_query": "",
            "query_params": {},
            "constraints": {},
        }
    if "price" in text or "价格" in text:
        service_type = "market_price"
    elif "report" in text or "报告" in text:
        service_type = "market_report"
    else:
        service_type = "generic_query"
    return {
        "is_payment_task": True,
        "service_type": service_type,
        "service_query": task_text,
        "query_params": {"q": task_text},
        "constraints": {"max_cost": 1.0},
    }


def default_service_registry() -> List[Dict[str, Any]]:
    return [
        {
            "name": "Mock Free Info",
            "service_type": "generic_query",
            "url": "http://127.0.0.1:18080/api/free",
            "http_method": "GET",
            "supports_x402": False,
            "reputation": "ok",
            "whitelist_tag": True,
            "headers": {},
            "body": None,
        },
        {
            "name": "Mock Weather Free",
            "service_type": "weather",
            "url": "http://127.0.0.1:18080/api/weather-free",
            "http_method": "GET",
            "supports_x402": False,
            "reputation": "ok",
            "whitelist_tag": True,
            "headers": {},
            "body": None,
        },
        {
            "name": "Mock Weather Pro",
            "service_type": "weather",
            "url": "http://127.0.0.1:18081/api/weather",
            "http_method": "GET",
            "supports_x402": True,
            "reputation": "ok",
            "whitelist_tag": True,
            "headers": {},
            "body": None,
        },
        {
            "name": "Mock Market Intel",
            "service_type": "market_report",
            "url": "http://127.0.0.1:18082/api/reports/coinbase",
            "http_method": "GET",
            "supports_x402": True,
            "reputation": "ok",
            "whitelist_tag": False,
            "headers": {},
            "body": None,
        },
    ]


def parse_service_identity(url: str) -> Dict[str, str]:
    parsed = urlparse(url)
    return {
        "service_domain": parsed.hostname or "",
        "service_path": parsed.path or "",
        "service_scheme": parsed.scheme or "",
    }

def default_state(
    target_url: Optional[str] = None,
    http_method: str = "GET",
    config: Optional[Dict[str, Any]] = None,
    task_text: Optional[str] = None,
) -> AgentState:
    cfg = config or {}
    return {
        "messages": [],
        "thread_id": str(cfg.get("thread_id", "")) or None,
        "payment_ctx": {
            "target_url": target_url,
            "http_method": http_method,
            "request_headers": {},
            "request_body": None,
            "status": "idle",
            "requirements": None,
            "requirements_raw": None,
            "selected_accept": None,
            "signed_payload": None,
            "amount_usdc": 0.0,
            "tx_hash": None,
            "last_http_status": None,
            "last_response_headers": None,
            "last_response_text": None,
            "error_type": None,
            "error_msg": None,
            "retry_count": 0,
            "network_retry_count": 0,
            "max_payment_retries": int(cfg.get("max_payment_retries", 3)),
            "max_network_retries": int(cfg.get("max_network_retries", 3)),
        },
        "http_client": None,
        "payment_preferences": {
            "network": str(cfg.get("preferred_network", "base")).lower(),
            "asset": str(cfg.get("preferred_asset", "usdc")).lower(),
            "scheme": str(cfg.get("preferred_scheme", "exact")).lower(),
        },
        "task_text": task_text,
        "intent": None,
        "service_candidates": [],
        "selected_service": None,
        "negotiation_result": None,
        "policy_decision": None,
        "reflection_notes": None,
        "risk_score": None,
        "risk_reasons": [],
        "llm_client": None,
        "policy": {
            "whitelist_domains": list(cfg.get("whitelist_domains", ())),
            "whitelist_services": list(cfg.get("whitelist_services", ())),
            "auto_pay_threshold": float(cfg.get("auto_approve_threshold", 0.1)),
            "hitl_threshold": float(cfg.get("hitl_threshold", 1.0)),
            "hard_limit": float(cfg.get("hard_limit", 10.0)),
            "daily_budget": float(cfg.get("budget_limit", 1.0)),
            "payee_blacklist": list(cfg.get("payee_blacklist", ())),
        },
        "wallet_balance": 0.0,
        "session_spend": 0.0,
        "budget_limit": float(cfg.get("budget_limit", 1.0)),
        "auto_approve_threshold": float(cfg.get("auto_approve_threshold", 0.1)),
        "human_approval_required": False,
        "user_decision": None,
        "risk_flags": {},
        "audit_log": [],
    }
