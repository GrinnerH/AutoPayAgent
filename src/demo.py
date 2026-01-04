import json
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from src.config import AgentConfig
from src.graph import build_graph
from src.nodes import set_llm_bundle
from src.llm import build_llm_bundle_from_env
from src.service_bootstrap import ensure_services_started
from src.utils import EthAccountWalletProvider, default_state


def _invoke_with_interrupts(graph, state, thread_id: str, auto_approve: bool):
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
    result = graph.invoke(state, config)
    while isinstance(result, dict) and "__interrupt__" in result:
        decision = "approve" if auto_approve else "reject"
        result = graph.invoke(Command(resume=decision), config)
    return result


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
    md_path = os.path.join("logs", f"{name}.md")
    audit = payload.get("audit_log", [])
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(f"# {name} execution log\n\n")
        handle.write("## Summary\n\n")
        handle.write(f"- status: {payload.get('status')}\n")
        handle.write(f"- error_type: {payload.get('error_type')}\n")
        handle.write(f"- intent: {json.dumps(payload.get('intent'), ensure_ascii=False)}\n")
        handle.write(f"- selected_service: {json.dumps(payload.get('selected_service'), ensure_ascii=False)}\n")
        handle.write("\n## Node Trace\n\n")
        for idx, entry in enumerate(audit, start=1):
            event = entry.get("event", "unknown")
            handle.write(f"### {idx}. {event}\n\n")
            handle.write("```json\n")
            handle.write(json.dumps(entry, ensure_ascii=False, indent=2))
            handle.write("\n```\n\n")
    print("saved log:", md_path)


def _init_md_log(name: str) -> None:
    os.makedirs("logs", exist_ok=True)
    md_path = os.path.join("logs", f"{name}.md")
    os.environ["LOG_MD_PATH"] = md_path
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(f"# {name} execution log\n\n")
        handle.write("## Node Trace\n\n")


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

def run_demo_1():
    ensure_services_started()
    config, graph, llm_bundle = _build_graph()
    print("Demo-1 场景：意图识别与分流（非支付任务）")
    state = default_state(config=config.__dict__, task_text="帮我概括这段文字：这是一个关于区块链的入门介绍。")
    state["wallet_balance"] = 10.0
    try:
        _init_md_log("demo-1")
        result = _invoke_with_interrupts(graph, state, "demo-1", auto_approve=True)
        print("demo-1 status:", result.get("payment_ctx", {}).get("status"))
        _save_result("demo-1", result)
    except Exception as exc:
        _save_error("demo-1", exc)
        raise


def run_demo_2():
    ensure_services_started()
    config, graph, llm_bundle = _build_graph()
    print("Demo-2 场景：服务发现与免费路径")
    state = default_state(config=config.__dict__, task_text="查询新加坡天气")
    state["wallet_balance"] = 10.0
    try:
        _init_md_log("demo-2")
        result = _invoke_with_interrupts(graph, state, "demo-2", auto_approve=True)
        print("demo-2 status:", result.get("payment_ctx", {}).get("status"))
        _save_result("demo-2", result)
    except Exception as exc:
        _save_error("demo-2", exc)
        raise


def run_demo_3():
    ensure_services_started()
    config, graph, llm_bundle = _build_graph()
    print("Demo-3 场景：小额自动支付")
    state = default_state(config=config.__dict__, task_text="查询新加坡实时降雨量")
    state["wallet_balance"] = 10.0
    try:
        _init_md_log("demo-3")
        result = _invoke_with_interrupts(graph, state, "demo-3", auto_approve=True)
        print("demo-3 status:", result.get("payment_ctx", {}).get("status"))
        _save_result("demo-3", result)
    except Exception as exc:
        _save_error("demo-3", exc)
        raise


def run_demo_4():
    ensure_services_started()
    config, graph, llm_bundle = _build_graph()
    print("Demo-4 场景：高额风险防控（HITL）")
    state = default_state(config=config.__dict__, task_text="下载一份web3行业的市场研究报告")
    state["wallet_balance"] = 10.0
    try:
        _init_md_log("demo-4")
        result = _invoke_with_interrupts(graph, state, "demo-4", auto_approve=True)
        print("demo-4 status:", result.get("payment_ctx", {}).get("status"))
        _save_result("demo-4", result)
    except Exception as exc:
        _save_error("demo-4", exc)
        raise


def run_demo_5():
    ensure_services_started()
    config, graph, llm_bundle = _build_graph()
    print("Demo-5 场景：支付反思与重试")
    state = default_state(
        config=config.__dict__,
        task_text="下载一份web3行业的市场研究报告",
    )
    state["payment_ctx"]["simulate_verification_failure"] = True
    state["wallet_balance"] = 10.0
    try:
        _init_md_log("demo-5")
        result = _invoke_with_interrupts(graph, state, "demo-5", auto_approve=True)
        print("demo-5 status:", result.get("payment_ctx", {}).get("status"))
        _save_result("demo-5", result)
    except Exception as exc:
        _save_error("demo-5", exc)
        raise


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
