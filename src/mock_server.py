import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _encode(obj):
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("utf-8")


class X402MockHandler(BaseHTTPRequestHandler):
    server_version = "X402Mock/0.1"

    def _send(self, status_code, headers=None, body=b""):
        self.send_response(status_code)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if not self.headers.get("PAYMENT-SIGNATURE"):
            requirements = {
                "x402Version": "1.0",
                "accepts": [
                    {
                        "network": "base",
                        "asset": "USDC",
                        "decimals": 6,
                        "amount": "50000",
                        "to": "0x1111111111111111111111111111111111111111",
                        "chainId": 8453,
                        "token_address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                        "scheme": "exact",
                    }
                ],
            }
            self._send(
                402,
                headers={"PAYMENT-REQUIRED": _encode(requirements)},
                body=b"payment required",
            )
            return

        receipt = {"txHash": "0xmocktxhash"}
        payload = json.dumps({"result": "ok"}).encode("utf-8")
        self._send(200, headers={"PAYMENT-RESPONSE": _encode(receipt)}, body=payload)

    def log_message(self, format, *args):
        return


def run_server(host="127.0.0.1", port=18080):
    server = ThreadingHTTPServer((host, port), X402MockHandler)
    print(f"x402 mock server listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()
