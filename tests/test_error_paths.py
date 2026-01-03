import base64
import json
import unittest
from typing import Any

import httpx

from utils import DummyWalletProvider, default_state
from workflow import run_workflow


def _encode(obj: Any) -> str:
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("utf-8")


def _requirements() -> dict:
    return {
        "x402Version": "1.0",
        "accepts": [
            {
                "network": "base",
                "asset": "USDC",
                "decimals": 6,
                "amount": "1000",
                "to": "0x1111111111111111111111111111111111111111",
                "chainId": 8453,
                "token_address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            }
        ],
    }


class ErrorPathTests(unittest.TestCase):
    def test_policy_reject(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=402,
                headers={"PAYMENT-REQUIRED": _encode(_requirements())},
                text="payment required",
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        state = default_state("https://mock.x402/resource")
        state["http_client"] = client
        state["wallet_balance"] = 10.0
        state["risk_flags"] = {"policy_blocked": True}

        final_state = run_workflow(state, DummyWalletProvider())
        self.assertEqual(final_state["payment_ctx"]["error_type"], "policy_reject")
        self.assertEqual(final_state["payment_ctx"]["status"], "failed")

    def test_payment_loop(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=402,
                headers={"PAYMENT-REQUIRED": _encode(_requirements())},
                text="payment required",
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        state = default_state("https://mock.x402/resource", config={"max_payment_retries": 1})
        state["http_client"] = client
        state["wallet_balance"] = 10.0
        state["payment_ctx"]["signed_payload"] = "dummy"

        final_state = run_workflow(state, DummyWalletProvider())
        self.assertEqual(final_state["payment_ctx"]["error_type"], "payment_loop")
        self.assertEqual(final_state["payment_ctx"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
