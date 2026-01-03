import hashlib
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from eth_account import Account
from eth_account.messages import encode_structured_data


app = FastAPI()
_nonce_cache: set[str] = set()


class VerifyRequest(BaseModel):
    paymentRequirements: Dict[str, Any]
    paymentPayload: Dict[str, Any]


class VerifyResponse(BaseModel):
    isValid: bool
    reason: Optional[str] = None
    normalized: Optional[Dict[str, Any]] = None
    checks: List[Dict[str, Any]] = Field(default_factory=list)


class SettleRequest(BaseModel):
    verifiedPayment: Dict[str, Any]


class SettleResponse(BaseModel):
    status: str
    settlementId: str
    txHash: str
    receipt: Dict[str, Any]


def _lower(value: Optional[str]) -> str:
    return str(value or "").lower()


def _find_matching_accept(requirements: Dict[str, Any], auth: Dict[str, Any], domain: Dict[str, Any], asset: Optional[str]) -> Optional[Dict[str, Any]]:
    accepts = requirements.get("accepts", []) or []
    for accept in accepts:
        if _lower(accept.get("network")) and _lower(accept.get("network")) != _lower(requirements.get("network", accept.get("network"))):
            continue
        if asset and _lower(accept.get("asset")) and _lower(accept.get("asset")) != _lower(asset):
            continue
        if _lower(accept.get("to")) != _lower(auth.get("to")):
            continue
        if str(accept.get("amount")) != str(auth.get("value")):
            continue
        if accept.get("chainId") and int(accept.get("chainId")) != int(domain.get("chainId", 0)):
            continue
        if accept.get("token_address") and _lower(accept.get("token_address")) != _lower(domain.get("verifyingContract")):
            continue
        return accept
    return None


def _verify_signature(payload: Dict[str, Any]) -> str:
    domain = payload.get("domain") or {}
    types = payload.get("types") or {}
    authorization = payload.get("authorization") or {}
    signature = payload.get("signature")
    if not signature:
        raise ValueError("missing_signature")
    data = {
        "types": types,
        "primaryType": "TransferWithAuthorization",
        "domain": domain,
        "message": authorization,
    }
    signable = encode_structured_data(data)
    return Account.recover_message(signable, signature=signature)


@app.post("/verify", response_model=VerifyResponse)
def verify_payment(request: VerifyRequest) -> VerifyResponse:
    requirements = request.paymentRequirements or {}
    payload = request.paymentPayload or {}

    checks: List[Dict[str, Any]] = []
    scheme = payload.get("scheme")
    if scheme != "exact":
        return VerifyResponse(isValid=False, reason="unsupported_scheme", checks=checks)

    inner = payload.get("payload") or {}
    authorization = inner.get("authorization") or {}
    domain = inner.get("domain") or {}

    if not authorization:
        return VerifyResponse(isValid=False, reason="missing_authorization", checks=checks)

    now = int(time.time())
    valid_after = int(authorization.get("validAfter", 0))
    valid_before = int(authorization.get("validBefore", 0))
    if now < valid_after:
        return VerifyResponse(isValid=False, reason="not_valid_yet", checks=checks)
    if valid_before and now > valid_before:
        return VerifyResponse(isValid=False, reason="expired", checks=checks)

    nonce_key = f"{_lower(authorization.get('from'))}:{authorization.get('nonce')}"
    if nonce_key in _nonce_cache:
        return VerifyResponse(isValid=False, reason="nonce_replay", checks=checks)

    try:
        recovered = _verify_signature(inner)
    except Exception as exc:
        return VerifyResponse(isValid=False, reason=f"bad_signature:{exc}", checks=checks)

    if _lower(recovered) != _lower(authorization.get("from")):
        return VerifyResponse(isValid=False, reason="signer_mismatch", checks=checks)

    match = _find_matching_accept(requirements, authorization, domain, inner.get("asset"))
    if not match:
        return VerifyResponse(isValid=False, reason="requirements_mismatch", checks=checks)

    _nonce_cache.add(nonce_key)

    normalized = {
        "from": authorization.get("from"),
        "to": authorization.get("to"),
        "value": authorization.get("value"),
        "nonce": authorization.get("nonce"),
        "network": payload.get("network"),
        "asset": inner.get("asset"),
    }
    return VerifyResponse(isValid=True, normalized=normalized, checks=checks)


@app.post("/settle", response_model=SettleResponse)
def settle_payment(request: SettleRequest) -> SettleResponse:
    verified = request.verifiedPayment or {}
    seed = f"{verified.get('from')}|{verified.get('to')}|{verified.get('value')}|{verified.get('nonce')}|{int(time.time())}"
    settlement_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    tx_hash = "0x" + hashlib.sha256(settlement_id.encode("utf-8")).hexdigest()[:64]
    receipt = {"txHash": tx_hash, "settlementId": settlement_id, "status": "settled"}
    return SettleResponse(status="settled", settlementId=settlement_id, txHash=tx_hash, receipt=receipt)
