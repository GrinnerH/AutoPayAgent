import base64
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional


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

        signature = self.headers.get("PAYMENT-SIGNATURE")
        if not signature:
            self._send(402, headers={"PAYMENT-REQUIRED": _encode(self._requirements(cfg))}, body=b"payment required")
            return

        if cfg.fail_first and not self.server.fail_used:  # type: ignore[attr-defined]
            self.server.fail_used = True  # type: ignore[attr-defined]
            self._send(402, headers={"PAYMENT-REQUIRED": _encode(self._requirements(cfg)), "X-REASON": "verification_failed"})
            return

        receipt = {"txHash": "0xmocktxhash", "network": cfg.network, "asset": cfg.asset}
        payload = json.dumps({"result": "ok", "service": cfg.name}).encode("utf-8")
        self._send(200, headers={"PAYMENT-RESPONSE": _encode(receipt)}, body=payload)

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
