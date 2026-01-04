import base64
import json
import os
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

import httpx


def _encode(obj: Any) -> str:
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("utf-8")


@dataclass
class ServiceConfig:
    name: str
    host: str
    port: int
    path: str
    amount_usdc: float
    payee: str
    asset: str = "USDC"
    network: str = "base"
    chain_id: int = 8453
    token_address: str = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    fail_first: bool = False
    free: bool = False


class X402MockHandler(BaseHTTPRequestHandler):
    server_version = "X402Mock/0.2"

    def _send(self, status_code: int, headers: Optional[Dict[str, str]] = None, body: bytes = b"") -> None:
        self.send_response(status_code)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:
        cfg: ServiceConfig = self.server.cfg  # type: ignore[attr-defined]

        if self.path != cfg.path:
            self._send(404, body=b"not found")
            return

        if cfg.free:
            body = {"result": "ok", "service": cfg.name, "paid": False}
            if "weather-free" in cfg.path:
                body.update({"temp_c": 29.0, "range": "today_only"})
            self._send(200, headers={"Content-Type": "application/json"}, body=json.dumps(body).encode("utf-8"))
            return

        requirements = self._requirements(cfg)
        signature = self.headers.get("PAYMENT-SIGNATURE")
        if not signature:
            self._send(402, headers={"PAYMENT-REQUIRED": _encode(requirements)}, body=b"payment required")
            return

        try:
            payload = json.loads(base64.b64decode(signature.encode("utf-8")).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            self._send(402, headers={"PAYMENT-REQUIRED": _encode(requirements), "X-REASON": "bad_signature"})
            return

        if cfg.fail_first and not self.server.fail_used:  # type: ignore[attr-defined]
            self.server.fail_used = True  # type: ignore[attr-defined]
            self._send(402, headers={"PAYMENT-REQUIRED": _encode(requirements), "X-REASON": "verification_failed"})
            return

        facilitator = os.getenv("FACILITATOR_URL", "http://127.0.0.1:9000")
        try:
            verify_resp = httpx.post(
                f"{facilitator}/verify",
                json={"paymentRequirements": requirements, "paymentPayload": payload},
                timeout=5.0,
            )
            verify_resp.raise_for_status()
        except httpx.HTTPError:
            if os.getenv("DEMO_DEBUG") == "1":
                print("mock_server verify failed: facilitator_unreachable", flush=True)
            self._send(402, headers={"PAYMENT-REQUIRED": _encode(requirements), "X-REASON": "facilitator_unreachable"})
            return

        verify_data = verify_resp.json()
        if not verify_data.get("isValid"):
            reason = verify_data.get("reason", "verification_failed")
            if os.getenv("DEMO_DEBUG") == "1":
                print(f"mock_server verify failed: {reason}", flush=True)
            self._send(402, headers={"PAYMENT-REQUIRED": _encode(requirements), "X-REASON": reason})
            return

        settle_resp = httpx.post(
            f"{facilitator}/settle",
            json={"verifiedPayment": verify_data.get("normalized", {})},
            timeout=5.0,
        )
        try:
            settle_resp.raise_for_status()
        except httpx.HTTPError:
            if os.getenv("DEMO_DEBUG") == "1":
                print("mock_server settle failed", flush=True)
            self._send(402, headers={"PAYMENT-REQUIRED": _encode(requirements), "X-REASON": "settle_failed"})
            return

        receipt = settle_resp.json().get("receipt") or {"txHash": "0xmocktxhash"}
        body = {"result": "ok", "service": cfg.name, "paid": True}
        if "/api/weather" in cfg.path:
            body.update(
                {
                    "hourly_temps": [28.8, 29.1, 29.4, 29.2],
                    "rain_mm": 3.2,
                    "range": "hourly_with_rain",
                }
            )
        elif "/api/reports/coinbase" in cfg.path:
            body.update({"summary": "Deep research report (paid).", "coverage": "full"})
        body = json.dumps(body).encode("utf-8")
        self._send(200, headers={"PAYMENT-RESPONSE": _encode(receipt)}, body=body)

    def _requirements(self, cfg: ServiceConfig) -> Dict[str, Any]:
        amount = int(cfg.amount_usdc * 1_000_000)
        return {
            "x402Version": "1.0",
            "accepts": [
                {
                    "network": cfg.network,
                    "asset": cfg.asset,
                    "decimals": 6,
                    "amount": str(amount),
                    "to": cfg.payee,
                    "chainId": cfg.chain_id,
                    "token_address": cfg.token_address,
                    "scheme": "exact",
                },
                {
                    "network": cfg.network,
                    "asset": cfg.asset,
                    "decimals": 6,
                    "amount": str(amount + 25000),
                    "to": cfg.payee,
                    "chainId": cfg.chain_id,
                    "token_address": cfg.token_address,
                    "scheme": "exact",
                    "note": "priority settlement",
                },
            ],
        }

    def log_message(self, format, *args) -> None:
        return


def start_server(config: ServiceConfig) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((config.host, config.port), X402MockHandler)
    server.cfg = config  # type: ignore[attr-defined]
    server.fail_used = False  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def run_server(host: str = "127.0.0.1", port: int = 18080) -> None:
    config = ServiceConfig(
        name="Mock Weather Pro",
        host=host,
        port=port,
        path="/api/weather",
        amount_usdc=0.05,
        payee="0x1111111111111111111111111111111111111111",
    )
    server = start_server(config)
    print(f"x402 mock server listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
