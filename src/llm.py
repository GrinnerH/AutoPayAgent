import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain.chat_models import init_chat_model
from langchain.messages import HumanMessage
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field

from tools import find_services, get_payee_reputation, get_wallet_balance, list_services


class IntentOutput(BaseModel):
    is_payment_task: bool = Field(default=True)
    service_type: str
    query_params: Dict[str, Any]
    constraints: Dict[str, Any]


class NegotiationOutput(BaseModel):
    selected_index: int = Field(default=0)
    reason: str
    policy_decision: str = Field(default="auto")


class ReflectionOutput(BaseModel):
    action: str
    reason: str
    notes: Dict[str, Any] = Field(default_factory=dict)


@dataclass
class AgentBundle:
    intent_agent: Any
    negotiation_agent: Any
    reflection_agent: Any

    def _invoke(self, agent: Any, messages: List[BaseMessage], thread_id: Optional[str]) -> Dict[str, Any]:
        config = {"configurable": {"thread_id": thread_id}} if thread_id else None
        if config:
            return agent.invoke({"messages": messages}, config)
        return agent.invoke({"messages": messages})

    def intent(
        self, messages: List[BaseMessage], task_text: str, thread_id: Optional[str]
    ) -> tuple[IntentOutput, List[BaseMessage]]:
        result = self._invoke(self.intent_agent, messages + [HumanMessage(task_text)], thread_id)
        return result["structured_response"], result.get("messages", messages)

    def negotiate(
        self, messages: List[BaseMessage], payload: Dict[str, Any], thread_id: Optional[str]
    ) -> tuple[NegotiationOutput, List[BaseMessage]]:
        result = self._invoke(self.negotiation_agent, messages + [HumanMessage(str(payload))], thread_id)
        return result["structured_response"], result.get("messages", messages)

    def reflect(
        self, messages: List[BaseMessage], payload: Dict[str, Any], thread_id: Optional[str]
    ) -> tuple[ReflectionOutput, List[BaseMessage]]:
        result = self._invoke(self.reflection_agent, messages + [HumanMessage(str(payload))], thread_id)
        return result["structured_response"], result.get("messages", messages)


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
            "Extract structured intent for paid API tasks."
        ),
        response_format=ToolStrategy(IntentOutput),
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
        negotiation_agent=negotiation_agent,
        reflection_agent=reflection_agent,
    )
