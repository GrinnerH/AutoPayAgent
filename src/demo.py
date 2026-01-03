import os
import threading
import time

from config import AgentConfig
from llm import build_llm_bundle_from_env
from mock_server import run_server
from utils import DummyWalletProvider, default_state
from workflow import run_workflow


def run_demo() -> None:
    server_thread = threading.Thread(
        target=run_server,
        kwargs={"host": "127.0.0.1", "port": 18080},
        daemon=True,
    )
    server_thread.start()
    time.sleep(0.2)

    config = AgentConfig(budget_limit=5.0, auto_approve_threshold=1.0, enable_llm=False)
    state = default_state("http://127.0.0.1:18080/resource", config=config.__dict__)
    state["wallet_balance"] = 10.0
    state["risk_flags"] = {"domain_trusted": True, "payee_reputation": "ok"}
    state["task_text"] = "查询 Coinbase 股价分析报告"

    if os.getenv("DEMO_ENABLE_LLM", "false").lower() in {"1", "true", "yes"}:
        state["llm_client"] = build_llm_bundle_from_env()
        state["thread_id"] = "demo-thread"

    final_state = run_workflow(state, DummyWalletProvider())
    print("final status:", final_state["payment_ctx"]["status"])
    print("tx_hash:", final_state["payment_ctx"]["tx_hash"])


if __name__ == "__main__":
    run_demo()
