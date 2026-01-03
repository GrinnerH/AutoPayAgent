import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain.chat_models import init_chat_model
from langchain_core.messages import BaseMessage, HumanMessage
from pydantic import BaseModel, Field

from tools import find_services, get_payee_reputation, get_wallet_balance, list_services


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
    action: str
    reason: str
    notes: Dict[str, Any] = Field(default_factory=dict)


@dataclass
class AgentBundle:
    intent_agent: Any
    service_agent: Any
    negotiation_agent: Any
    reflection_agent: Any

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
    ) -> tuple[IntentOutput, List[BaseMessage]]:
        history = messages + [HumanMessage(task_text)]
        result = self._invoke(self.intent_agent, history, thread_id)
        return result["structured_response"], self._delta_messages(history, result)

    def negotiate(
        self, messages: List[BaseMessage], payload: Dict[str, Any], thread_id: Optional[str]
    ) -> tuple[NegotiationOutput, List[BaseMessage]]:
        history = messages + [HumanMessage(str(payload))]
        result = self._invoke(self.negotiation_agent, history, thread_id)
        return result["structured_response"], self._delta_messages(history, result)

    def select_service(
        self, messages: List[BaseMessage], payload: Dict[str, Any], thread_id: Optional[str]
    ) -> tuple[ServiceSelectionOutput, List[BaseMessage]]:
        history = messages + [HumanMessage(str(payload))]
        result = self._invoke(self.service_agent, history, thread_id)
        return result["structured_response"], self._delta_messages(history, result)

    def reflect(
        self, messages: List[BaseMessage], payload: Dict[str, Any], thread_id: Optional[str]
    ) -> tuple[ReflectionOutput, List[BaseMessage]]:
        history = messages + [HumanMessage(str(payload))]
        result = self._invoke(self.reflection_agent, history, thread_id)
        return result["structured_response"], self._delta_messages(history, result)


def _normalize_model_name(model_name: str) -> str:
    if ":" in model_name:
        return model_name
    return f"openai:{model_name}"


def build_llm_bundle_from_env() -> Optional[AgentBundle]:
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    model_name = os.getenv("MODEL_NAME")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    if not api_key or not model_name:
        return None

    os.environ.setdefault("OPENAI_API_KEY", api_key)
    os.environ.setdefault("OPENAI_BASE_URL", base_url)

    model = init_chat_model(_normalize_model_name(model_name), temperature=0)

    intent_agent = create_agent(
        model=model,
        tools=[list_services, find_services],
        system_prompt=(
            "You are an intent extraction agent. "
            "Extract structured intent for paid API tasks. "
            "Include a service_query suitable for service discovery."
        ),
        response_format=ToolStrategy(IntentOutput),
    )

    service_agent = create_agent(
        model=model,
        tools=[list_services, find_services],
        system_prompt=(
            "You are a service selection agent. "
            "Use tools to find candidate services and pick the best match."
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
            "Explain failures and propose the next action."
        ),
        response_format=ToolStrategy(ReflectionOutput),
    )

    return AgentBundle(
        intent_agent=intent_agent,
        service_agent=service_agent,
        negotiation_agent=negotiation_agent,
        reflection_agent=reflection_agent,
    )
