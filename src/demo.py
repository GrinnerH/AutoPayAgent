import os
import sys
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


def _start_facilitator():
    facilitator_host = os.getenv("FACILITATOR_HOST", "127.0.0.1")
    facilitator_port = int(os.getenv("FACILITATOR_PORT", "9000"))
    facilitator_thread = threading.Thread(
        target=uvicorn.run,
        kwargs={"app": facilitator_app, "host": facilitator_host, "port": facilitator_port, "log_level": "warning"},
        daemon=True,
    )
    facilitator_thread.start()
    os.environ.setdefault("FACILITATOR_URL", f"http://{facilitator_host}:{facilitator_port}")
    return facilitator_thread

def _build_graph():
    config = AgentConfig(budget_limit=5.0, auto_approve_threshold=1.0, enable_llm=False, enable_checkpoint=True)
    wallet = EthAccountWalletProvider.from_env()
    checkpointer = InMemorySaver()
    graph = build_graph(wallet, checkpointer=checkpointer)
    llm_bundle = build_llm_bundle_from_env()
    if llm_bundle is None:
        raise RuntimeError("LLM is required for demos; set OPENAI_API_KEY and MODEL_NAME in .env.")
    return config, graph, llm_bundle


def run_demo_1():
    _start_facilitator()
    config, graph, llm_bundle = _build_graph()
    print("Demo-1 场景：意图识别与分流（非支付任务）")
    state = default_state(config=config.__dict__, task_text="1 比特币等于多少美金？")
    state["wallet_balance"] = 10.0
    state["llm_client"] = llm_bundle
    result = _invoke_with_interrupts(graph, state, "demo-1", auto_approve=True)
    print("demo-1 status:", result.get("payment_ctx", {}).get("status"))


def run_demo_2():
    _start_facilitator()
    free_server = start_server(
        ServiceConfig(
            name="Mock Weather Free",
            host="127.0.0.1",
            port=18080,
            path="/api/weather-free",
            amount_usdc=0.0,
            payee="0x0000000000000000000000000000000000000000",
            free=True,
        )
    )
    time.sleep(0.2)
    config, graph, llm_bundle = _build_graph()
    print("Demo-2 场景：服务发现与免费路径")
    state = default_state(config=config.__dict__, task_text="查询新加坡天气")
    state["wallet_balance"] = 10.0
    state["llm_client"] = llm_bundle
    result = _invoke_with_interrupts(graph, state, "demo-2", auto_approve=True)
    print("demo-2 status:", result.get("payment_ctx", {}).get("status"))
    free_server.shutdown()


def run_demo_3():
    _start_facilitator()
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
    time.sleep(0.2)
    config, graph, llm_bundle = _build_graph()
    print("Demo-3 场景：小额自动支付")
    state = default_state(config=config.__dict__, task_text="查询新加坡实时降雨量")
    state["wallet_balance"] = 10.0
    state["llm_client"] = llm_bundle
    result = _invoke_with_interrupts(graph, state, "demo-3", auto_approve=True)
    print("demo-3 status:", result.get("payment_ctx", {}).get("status"))
    weather_server.shutdown()


def run_demo_4():
    _start_facilitator()
    market_server = start_server(
        ServiceConfig(
            name="Mock Market Intel",
            host="127.0.0.1",
            port=18082,
            path="/api/reports/coinbase",
            amount_usdc=5.00,
            payee="0x2222222222222222222222222222222222222222",
        )
    )
    time.sleep(0.2)
    config, graph, llm_bundle = _build_graph()
    print("Demo-4 场景：高额风险防控（HITL）")
    state = default_state(config=config.__dict__, task_text="下载一份价值 5 USDC 的深度研究报告")
    state["wallet_balance"] = 10.0
    state["llm_client"] = llm_bundle
    result = _invoke_with_interrupts(graph, state, "demo-4", auto_approve=True)
    print("demo-4 status:", result.get("payment_ctx", {}).get("status"))
    market_server.shutdown()


def run_demo_5():
    _start_facilitator()
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
    config, graph, llm_bundle = _build_graph()
    print("Demo-5 场景：支付反思与重试")
    state = default_state(
        target_url="http://127.0.0.1:18083/api/reports/coinbase",
        config=config.__dict__,
        task_text="测试支付失败后的反思重试",
    )
    state["wallet_balance"] = 10.0
    state["llm_client"] = llm_bundle
    result = _invoke_with_interrupts(graph, state, "demo-5", auto_approve=True)
    print("demo-5 status:", result.get("payment_ctx", {}).get("status"))
    fail_server.shutdown()


def run_demo() -> None:
    scenarios = {
        "demo-1": run_demo_1,
        "demo-2": run_demo_2,
        "demo-3": run_demo_3,
        "demo-4": run_demo_4,
        "demo-5": run_demo_5,
    }
    if len(sys.argv) < 2:
        print("Usage: python3 src/demo.py <demo-1|demo-2|demo-3|demo-4|demo-5>")
        print("Available:", ", ".join(sorted(scenarios.keys())))
        return
    name = sys.argv[1].strip().lower()
    runner = scenarios.get(name)
    if not runner:
        print("Unknown scenario:", name)
        print("Available:", ", ".join(sorted(scenarios.keys())))
        return
    runner()


if __name__ == "__main__":
    run_demo()
