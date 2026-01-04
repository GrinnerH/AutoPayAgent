import os
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain.chat_models import init_chat_model
from langchain_core.messages import BaseMessage, HumanMessage
from pydantic import BaseModel, Field

from src.tools import find_services, get_payee_reputation, get_wallet_balance, list_services


class IntentOutput(BaseModel):
    is_payment_task: bool = Field(default=True)
    service_type: str
    service_query: str = Field(default="")
    query_params: Dict[str, Any]
    constraints: Dict[str, Any]


class NegotiationOutput(BaseModel):
    selected_index: int = Field(default=0)
    reason: str
    policy_decision: str = Field(default="auto")


class ServiceSelectionOutput(BaseModel):
    selected_index: int = Field(default=0)
    service_name: Optional[str] = None
    reason: str


class ReflectionOutput(BaseModel):
    action: Literal["retry_request", "reselect_accept", "human_approval", "abort"]
    reason: str
    notes: Dict[str, Any] = Field(default_factory=dict)


class NormalChatOutput(BaseModel):
    response: str


@dataclass
class AgentBundle:
    intent_agent: Any
    service_agent: Any
    negotiation_agent: Any
    reflection_agent: Any
    chat_agent: Any

    def _invoke(self, agent: Any, messages: List[BaseMessage], thread_id: Optional[str]) -> Dict[str, Any]:
        config = {"configurable": {"thread_id": thread_id}} if thread_id else None
        if config:
            return agent.invoke({"messages": messages}, config)
        return agent.invoke({"messages": messages})

    def _delta_messages(self, history: List[BaseMessage], result: Dict[str, Any]) -> List[BaseMessage]:
        new_messages = result.get("messages", [])
        if not new_messages:
            return []
        if len(new_messages) >= len(history) and new_messages[: len(history)] == history:
            return new_messages[len(history) :]
        return new_messages

    def intent(
        self, messages: List[BaseMessage], task_text: str, thread_id: Optional[str]
    ) -> tuple[IntentOutput, List[BaseMessage], Dict[str, Any]]:
        history = messages + [HumanMessage(task_text)]
        result = self._invoke(self.intent_agent, history, thread_id)
        return result["structured_response"], self._delta_messages(history, result), result

    def negotiate(
        self, messages: List[BaseMessage], payload: Dict[str, Any], thread_id: Optional[str]
    ) -> tuple[NegotiationOutput, List[BaseMessage], Dict[str, Any]]:
        history = messages + [HumanMessage(str(payload))]
        result = self._invoke(self.negotiation_agent, history, thread_id)
        return result["structured_response"], self._delta_messages(history, result), result

    def select_service(
        self, messages: List[BaseMessage], payload: Dict[str, Any], thread_id: Optional[str]
    ) -> tuple[ServiceSelectionOutput, List[BaseMessage], Dict[str, Any]]:
        history = messages + [HumanMessage(str(payload))]
        result = self._invoke(self.service_agent, history, thread_id)
        return result["structured_response"], self._delta_messages(history, result), result

    def reflect(
        self, messages: List[BaseMessage], payload: Dict[str, Any], thread_id: Optional[str]
    ) -> tuple[ReflectionOutput, List[BaseMessage], Dict[str, Any]]:
        history = messages + [HumanMessage(str(payload))]
        result = self._invoke(self.reflection_agent, history, thread_id)
        return result["structured_response"], self._delta_messages(history, result), result

    def normal_chat(
        self, messages: List[BaseMessage], task_text: str, thread_id: Optional[str]
    ) -> tuple[NormalChatOutput, List[BaseMessage], Dict[str, Any]]:
        history = messages + [HumanMessage(task_text)]
        result = self._invoke(self.chat_agent, history, thread_id)
        return result["structured_response"], self._delta_messages(history, result), result


def _normalize_model_name(model_name: str) -> str:
    if ":" in model_name:
        return model_name
    return f"openai:{model_name}"


def build_llm_bundle_from_env() -> Optional[AgentBundle]:
    load_dotenv()
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        load_dotenv(env_path)
    api_key = os.getenv("OPENAI_API_KEY")
    model_name = os.getenv("MODEL_NAME")
    base_url = os.getenv("BASE_URL") or os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    base_url = base_url.rstrip("/")
    if not api_key or not model_name:
        return None

    model = init_chat_model(
        _normalize_model_name(model_name),
        temperature=0,
        base_url=base_url,
        api_key=api_key,
        timeout=30,
        max_retries=0,
        streaming=False,
    )

    intent_agent = create_agent(
        model=model,
        tools=[list_services, find_services],
        system_prompt=(
            "You are an intent extraction agent. "
            "Decide if the task truly requires a paid external API. "
            "If the question can be answered from general knowledge (e.g., BTC price, general facts, summarization), "
            "set is_payment_task=false. "
            "Only set is_payment_task=true when the user explicitly requests external data access or a paid service. "
            "If the user explicitly says to use paid services, set constraints.paid_required=true. "
            "If the user mentions a price/cost or says a report costs money, set constraints.paid_required=true. "
            "If the user asks to test verification failure, reflection, or retry after payment failure, "
            "set constraints.require_retry_service=true and service_type=market_report_retry. "
            "If the user asks to call a specific API/service endpoint or to perform a paid request, set is_payment_task=true. "
            "Include a service_query suitable for service discovery only when is_payment_task=true."
        ),
        response_format=ToolStrategy(IntentOutput),
    )

    service_agent = create_agent(
        model=model,
        tools=[list_services, find_services],
        system_prompt=(
            "You are a service selection agent. "
            "Use tools to find candidate services and pick the best match. "
            "Prefer free services when they satisfy the task."
        ),
        response_format=ToolStrategy(ServiceSelectionOutput),
    )

    negotiation_agent = create_agent(
        model=model,
        tools=[get_wallet_balance],
        system_prompt=(
            "You are a payment negotiation agent. "
            "Choose the best accept option and return a structured decision."
        ),
        response_format=ToolStrategy(NegotiationOutput),
    )

    reflection_agent = create_agent(
        model=model,
        tools=[get_payee_reputation],
        system_prompt=(
            "You are a payment reflection agent. "
            "Explain failures and propose the next action. "
            "Allowed actions: retry_request, reselect_accept, human_approval, abort. "
            "Use retry_request for transient verification failures when retry_count is low."
        ),
        response_format=ToolStrategy(ReflectionOutput),
    )

    chat_agent = create_agent(
        model=model,
        tools=[],
        system_prompt=(
            "You are a helpful assistant. "
            "Answer the user's question directly and concisely."
        ),
        response_format=ToolStrategy(NormalChatOutput),
    )

    return AgentBundle(
        intent_agent=intent_agent,
        service_agent=service_agent,
        negotiation_agent=negotiation_agent,
        reflection_agent=reflection_agent,
        chat_agent=chat_agent,
    )
