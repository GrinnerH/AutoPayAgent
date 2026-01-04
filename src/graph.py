from langgraph.graph import END, START, StateGraph

from src.nodes import (
    error_handler,
    header_parser,
    human_approver,
    intent_router,
    normal_chat,
    payment_signer,
    payment_negotiator,
    request_executor,
    reflection_rewriter,
    response_validator,
    risk_assessor,
    service_registry,
)
from src.utils import AgentState, WalletProvider


def route_after_request(state: AgentState) -> str:
    ctx = state["payment_ctx"]
    status = ctx.get("last_http_status")
    error_type = ctx.get("error_type")

    if status == 402 and error_type in {"payment_verification_failed", "payment_loop"}:
        return "error_handler"
    if status == 402:
        return "header_parser"
    if status == 200:
        return "response_validator"
    if error_type:
        return "error_handler"
    return END


def route_after_intent(state: AgentState) -> str:
    intent = state.get("intent") or {}
    if intent.get("is_payment_task") is False:
        return "normal_chat"
    return "service_registry"


def route_after_risk(state: AgentState) -> str:
    decision = state.get("policy_decision")
    if decision == "reject":
        return "error_handler"
    if decision == "hitl":
        return "human_approver"
    if decision == "auto":
        return "payment_signer"
    if state["human_approval_required"]:
        return "human_approver"
    return "payment_signer"


def route_after_human(state: AgentState) -> str:
    decision = state.get("user_decision")
    if decision == "approve":
        return "payment_signer"
    return "error_handler"


def route_after_error(state: AgentState) -> str:
    ctx = state["payment_ctx"]
    error_type = ctx.get("error_type")
    if error_type in {"network_error", "http_server_error"} and ctx["network_retry_count"] < ctx["max_network_retries"]:
        return "request_executor"
    if error_type in {"payment_verification_failed", "payment_loop"} and ctx["retry_count"] < ctx["max_payment_retries"]:
        return "reflection_rewriter"
    return END


def route_after_reflection(state: AgentState) -> str:
    notes = state.get("reflection_notes") or {}
    action = notes.get("action")
    if action == "reselect_accept":
        return "payment_negotiator"
    if action == "retry_request":
        return "request_executor"
    if action == "human_approval" or notes.get("force_human"):
        return "human_approver"
    return END


def build_payment_subgraph(wallet_provider: WalletProvider):
    builder = StateGraph(AgentState)
    builder.add_node("request_executor", request_executor)
    builder.add_node("header_parser", header_parser)
    builder.add_node("payment_negotiator", payment_negotiator)
    builder.add_node("risk_assessor", risk_assessor)
    builder.add_node("human_approver", human_approver)
    builder.add_node("payment_signer", lambda state: payment_signer(state, wallet_provider))
    builder.add_node("response_validator", response_validator)
    builder.add_node("error_handler", error_handler)
    builder.add_node("reflection_rewriter", reflection_rewriter)

    builder.add_edge(START, "request_executor")
    builder.add_conditional_edges(
        "request_executor",
        route_after_request,
        {
            "header_parser": "header_parser",
            "response_validator": "response_validator",
            "error_handler": "error_handler",
            END: END,
        },
    )
    builder.add_edge("header_parser", "payment_negotiator")
    builder.add_edge("payment_negotiator", "risk_assessor")
    builder.add_conditional_edges(
        "risk_assessor",
        route_after_risk,
        {
            "error_handler": "error_handler",
            "human_approver": "human_approver",
            "payment_signer": "payment_signer",
        },
    )
    builder.add_conditional_edges(
        "human_approver",
        route_after_human,
        {
            "payment_signer": "payment_signer",
            "error_handler": "error_handler",
        },
    )
    builder.add_edge("payment_signer", "request_executor")
    builder.add_edge("response_validator", END)
    builder.add_conditional_edges(
        "error_handler",
        route_after_error,
        {
            "request_executor": "request_executor",
            "reflection_rewriter": "reflection_rewriter",
            END: END,
        },
    )
    builder.add_conditional_edges(
        "reflection_rewriter",
        route_after_reflection,
        {
            "payment_negotiator": "payment_negotiator",
            "request_executor": "request_executor",
            "human_approver": "human_approver",
            END: END,
        },
    )
    return builder.compile()


def build_graph(wallet_provider: WalletProvider, checkpointer=None):
    builder = StateGraph(AgentState)
    builder.add_node("intent_router", intent_router)
    builder.add_node("service_registry", service_registry)
    builder.add_node("normal_chat", normal_chat)
    builder.add_node("payment_subgraph", build_payment_subgraph(wallet_provider))

    builder.add_edge(START, "intent_router")
    builder.add_conditional_edges(
        "intent_router",
        route_after_intent,
        {
            "normal_chat": "normal_chat",
            "service_registry": "service_registry",
        },
    )
    builder.add_edge("normal_chat", END)
    builder.add_edge("service_registry", "payment_subgraph")
    builder.add_edge("payment_subgraph", END)

    return builder.compile(checkpointer=checkpointer)
