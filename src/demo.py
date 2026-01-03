import os
import threading
import time

import uvicorn
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from config import AgentConfig
from facilitator_server import app as facilitator_app
from graph import build_graph
from llm import build_llm_bundle_from_env
from mock_server import ServiceConfig, start_server
from utils import EthAccountWalletProvider, default_state


def _invoke_with_interrupts(graph, state, thread_id: str, auto_approve: bool):
    result = graph.invoke(state, {"configurable": {"thread_id": thread_id}})
    while isinstance(result, dict) and "__interrupt__" in result:
        decision = "approve" if auto_approve else "reject"
        result = graph.invoke(Command(resume=decision), {"configurable": {"thread_id": thread_id}})
    return result


def run_demo() -> None:
    facilitator_host = os.getenv("FACILITATOR_HOST", "127.0.0.1")
    facilitator_port = int(os.getenv("FACILITATOR_PORT", "9000"))
    facilitator_thread = threading.Thread(
        target=uvicorn.run,
        kwargs={"app": facilitator_app, "host": facilitator_host, "port": facilitator_port, "log_level": "warning"},
        daemon=True,
    )
    facilitator_thread.start()
    os.environ.setdefault("FACILITATOR_URL", f"http://{facilitator_host}:{facilitator_port}")

    weather_server = start_server(
        ServiceConfig(
            name="Mock Weather Pro",
            host="127.0.0.1",
            port=18081,
            path="/api/weather",
            amount_usdc=0.05,
            payee="0x1111111111111111111111111111111111111111",
        )
    )
    market_server = start_server(
        ServiceConfig(
            name="Mock Market Intel",
            host="127.0.0.1",
            port=18082,
            path="/api/reports/coinbase",
            amount_usdc=2.50,
            payee="0x2222222222222222222222222222222222222222",
        )
    )
    fail_server = start_server(
        ServiceConfig(
            name="Mock Market Intel Retry",
            host="127.0.0.1",
            port=18083,
            path="/api/reports/coinbase",
            amount_usdc=0.25,
            payee="0x3333333333333333333333333333333333333333",
            fail_first=True,
        )
    )

    time.sleep(0.2)

    config = AgentConfig(budget_limit=5.0, auto_approve_threshold=1.0, enable_llm=False, enable_checkpoint=True)
    wallet = EthAccountWalletProvider.from_env()
    checkpointer = InMemorySaver()
    graph = build_graph(wallet, checkpointer=checkpointer)

    llm_bundle = None
    if os.getenv("DEMO_ENABLE_LLM", "false").lower() in {"1", "true", "yes"}:
        llm_bundle = build_llm_bundle_from_env()

    # Scenario A: small amount auto-pay (weather)
    state_a = default_state(config=config.__dict__, task_text="帮我查询今天新加坡的天气（可以付费）")
    state_a["wallet_balance"] = 10.0
    state_a["llm_client"] = llm_bundle
    result_a = _invoke_with_interrupts(graph, state_a, "demo-a", auto_approve=True)
    print("scenario A status:", result_a["payment_ctx"]["status"])

    # Scenario B: higher amount triggers HITL (market report)
    state_b = default_state(config=config.__dict__, task_text="查询 Coinbase 股价分析报告（可能较贵）")
    state_b["wallet_balance"] = 10.0
    state_b["llm_client"] = llm_bundle
    result_b = _invoke_with_interrupts(graph, state_b, "demo-b", auto_approve=True)
    print("scenario B status:", result_b["payment_ctx"]["status"])

    # Scenario C: failure then reflection (direct target_url to fail-first server)
    state_c = default_state(
        target_url="http://127.0.0.1:18083/api/reports/coinbase",
        config=config.__dict__,
        task_text="测试支付失败后的反思重试",
    )
    state_c["wallet_balance"] = 10.0
    state_c["llm_client"] = llm_bundle
    result_c = _invoke_with_interrupts(graph, state_c, "demo-c", auto_approve=True)
    print("scenario C status:", result_c["payment_ctx"]["status"])

    weather_server.shutdown()
    market_server.shutdown()
    fail_server.shutdown()


if __name__ == "__main__":
    run_demo()
