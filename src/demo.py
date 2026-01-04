import json
import os
import sys
import threading
import time

import httpx
import uvicorn
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from src.config import AgentConfig
from src.facilitator_server import app as facilitator_app
from src.graph import build_graph
from src.nodes import set_llm_bundle
from src.llm import build_llm_bundle_from_env
from src.mock_server import ServiceConfig, start_server
from src.utils import EthAccountWalletProvider, default_state


def _invoke_with_interrupts(graph, state, thread_id: str, auto_approve: bool):
    result = graph.invoke(state, {"configurable": {"thread_id": thread_id}})
    while isinstance(result, dict) and "__interrupt__" in result:
        decision = "approve" if auto_approve else "reject"
        result = graph.invoke(Command(resume=decision), {"configurable": {"thread_id": thread_id}})
    return result


def _start_facilitator():
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["LANGCHAIN_TRACING"] = "false"
    facilitator_host = os.getenv("FACILITATOR_HOST", "127.0.0.1")
    facilitator_port = int(os.getenv("FACILITATOR_PORT", "9000"))
    base_url = f"http://{facilitator_host}:{facilitator_port}"
    try:
        resp = httpx.get(f"{base_url}/openapi.json", timeout=1.0)
        if resp.status_code == 200:
            os.environ.setdefault("FACILITATOR_URL", base_url)
            return None
    except httpx.HTTPError:
        pass
    facilitator_thread = threading.Thread(
        target=uvicorn.run,
        kwargs={"app": facilitator_app, "host": facilitator_host, "port": facilitator_port, "log_level": "warning"},
        daemon=True,
    )
    facilitator_thread.start()
    os.environ.setdefault("FACILITATOR_URL", base_url)
    for _ in range(10):
        try:
            if httpx.get(f"{base_url}/openapi.json", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    return facilitator_thread

def _build_graph():
    config = AgentConfig(budget_limit=5.0, auto_approve_threshold=1.0, enable_llm=False, enable_checkpoint=True)
    wallet = EthAccountWalletProvider.from_env()
    checkpointer = InMemorySaver()
    graph = build_graph(wallet, checkpointer=checkpointer)
    llm_bundle = build_llm_bundle_from_env()
    if llm_bundle is None:
        raise RuntimeError("LLM is required for demos; set OPENAI_API_KEY and MODEL_NAME in .env.")
    set_llm_bundle(llm_bundle)
    return config, graph, llm_bundle


def _save_result(name: str, result: dict) -> None:
    os.makedirs("logs", exist_ok=True)
    messages = result.get("messages", [])
    serialized_messages = []
    for message in messages:
        try:
            msg_type = getattr(message, "type", message.__class__.__name__)
            serialized_messages.append(
                {
                    "type": msg_type,
                    "content": getattr(message, "content", str(message)),
                    "additional_kwargs": getattr(message, "additional_kwargs", {}),
                }
            )
        except Exception:
            serialized_messages.append({"type": "unknown", "content": str(message), "additional_kwargs": {}})
    payload = {
        "status": result.get("payment_ctx", {}).get("status"),
        "error_type": result.get("payment_ctx", {}).get("error_type"),
        "audit_log": result.get("audit_log", []),
        "intent": result.get("intent"),
        "selected_service": result.get("selected_service"),
        "policy": result.get("policy"),
        "payment_ctx": result.get("payment_ctx"),
        "messages": serialized_messages,
    }
    path = os.path.join("logs", f"{name}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print("saved log:", path)


def _save_error(name: str, exc: Exception) -> None:
    os.makedirs("logs", exist_ok=True)
    payload = {
        "error": type(exc).__name__,
        "message": str(exc),
    }
    path = os.path.join("logs", f"{name}.error.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print("saved error log:", path)

def _start_demo_services() -> list:
    servers = []
    servers.append(
        start_server(
            ServiceConfig(
                name="Mock Free Info",
                host="127.0.0.1",
                port=18079,
                path="/api/free",
                amount_usdc=0.0,
                payee="0x0000000000000000000000000000000000000000",
                free=True,
            )
        )
    )
    servers.append(
        start_server(
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
    )
    servers.append(
        start_server(
            ServiceConfig(
                name="Mock Weather Pro",
                host="127.0.0.1",
                port=18081,
                path="/api/weather",
                amount_usdc=0.05,
                payee="0x1111111111111111111111111111111111111111",
            )
        )
    )
    servers.append(
        start_server(
            ServiceConfig(
                name="Mock Market Intel",
                host="127.0.0.1",
                port=18082,
                path="/api/reports/coinbase",
                amount_usdc=5.00,
                payee="0x2222222222222222222222222222222222222222",
            )
        )
    )
    servers.append(
        start_server(
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
    )
    time.sleep(0.2)
    return servers

def _stop_demo_services(servers: list) -> None:
    for server in servers:
        try:
            server.shutdown()
        except Exception:
            pass

def run_demo_1():
    _start_facilitator()
    config, graph, llm_bundle = _build_graph()
    print("Demo-1 场景：意图识别与分流（非支付任务）")
    state = default_state(config=config.__dict__, task_text="帮我概括这段文字：这是一个关于区块链的入门介绍。")
    state["wallet_balance"] = 10.0
    try:
        result = _invoke_with_interrupts(graph, state, "demo-1", auto_approve=True)
        print("demo-1 status:", result.get("payment_ctx", {}).get("status"))
        _save_result("demo-1", result)
    except Exception as exc:
        _save_error("demo-1", exc)
        raise


def run_demo_2():
    _start_facilitator()
    servers = _start_demo_services()
    config, graph, llm_bundle = _build_graph()
    print("Demo-2 场景：服务发现与免费路径")
    state = default_state(config=config.__dict__, task_text="查询新加坡天气")
    state["wallet_balance"] = 10.0
    try:
        result = _invoke_with_interrupts(graph, state, "demo-2", auto_approve=True)
        print("demo-2 status:", result.get("payment_ctx", {}).get("status"))
        _save_result("demo-2", result)
    except Exception as exc:
        _save_error("demo-2", exc)
        raise
    _stop_demo_services(servers)


def run_demo_3():
    _start_facilitator()
    servers = _start_demo_services()
    config, graph, llm_bundle = _build_graph()
    print("Demo-3 场景：小额自动支付")
    state = default_state(config=config.__dict__, task_text="查询新加坡实时降雨量（使用付费服务）")
    state["wallet_balance"] = 10.0
    try:
        result = _invoke_with_interrupts(graph, state, "demo-3", auto_approve=True)
        print("demo-3 status:", result.get("payment_ctx", {}).get("status"))
        _save_result("demo-3", result)
    except Exception as exc:
        _save_error("demo-3", exc)
        raise
    _stop_demo_services(servers)


def run_demo_4():
    _start_facilitator()
    servers = _start_demo_services()
    config, graph, llm_bundle = _build_graph()
    print("Demo-4 场景：高额风险防控（HITL）")
    state = default_state(config=config.__dict__, task_text="下载一份价值 5 USDC 的深度研究报告")
    state["wallet_balance"] = 10.0
    try:
        result = _invoke_with_interrupts(graph, state, "demo-4", auto_approve=True)
        print("demo-4 status:", result.get("payment_ctx", {}).get("status"))
        _save_result("demo-4", result)
    except Exception as exc:
        _save_error("demo-4", exc)
        raise
    _stop_demo_services(servers)


def run_demo_5():
    _start_facilitator()
    servers = _start_demo_services()
    config, graph, llm_bundle = _build_graph()
    print("Demo-5 场景：支付反思与重试")
    state = default_state(
        config=config.__dict__,
        task_text="调用付费服务获取研究报告，并在支付验证失败时进行反思重试",
    )
    state["wallet_balance"] = 10.0
    try:
        result = _invoke_with_interrupts(graph, state, "demo-5", auto_approve=True)
        print("demo-5 status:", result.get("payment_ctx", {}).get("status"))
        _save_result("demo-5", result)
    except Exception as exc:
        _save_error("demo-5", exc)
        raise
    _stop_demo_services(servers)


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
