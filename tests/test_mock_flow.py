import base64
import json
import unittest
from typing import Any

import httpx

from utils import DummyWalletProvider, default_state
from workflow import run_workflow


def _encode(obj: Any) -> str:
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("utf-8")


class MockFlowTest(unittest.TestCase):
    def test_mock_flow_success(self) -> None:
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
                }
            ],
        }
        receipt = {"txHash": "0xmocktxhash"}

        def handler(request: httpx.Request) -> httpx.Response:
            if "PAYMENT-SIGNATURE" not in request.headers:
                return httpx.Response(
                    status_code=402,
                    headers={"PAYMENT-REQUIRED": _encode(requirements)},
                    text="payment required",
                )
            return httpx.Response(
                status_code=200,
                headers={"PAYMENT-RESPONSE": _encode(receipt)},
                json={"result": "ok"},
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        state = default_state("https://mock.x402/resource")
        state["http_client"] = client
        state["wallet_balance"] = 10.0
        state["budget_limit"] = 5.0
        state["auto_approve_threshold"] = 1.0
        state["risk_flags"] = {"domain_trusted": True, "payee_reputation": "ok"}

        final_state = run_workflow(state, DummyWalletProvider())

        self.assertEqual(final_state["payment_ctx"]["status"], "success")
        self.assertEqual(final_state["payment_ctx"]["tx_hash"], "0xmocktxhash")


if __name__ == "__main__":
    unittest.main()
