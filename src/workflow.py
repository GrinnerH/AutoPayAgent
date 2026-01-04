import os
import sqlite3
from dataclasses import asdict
from typing import Optional

from dotenv import load_dotenv
from src.config import AgentConfig
from src.graph import build_graph
from src.utils import AgentKitWalletProvider, AgentState, WalletProvider, default_state
from langgraph.checkpoint.memory import InMemorySaver
from src.llm import build_llm_bundle_from_env


def run_workflow(state: AgentState, wallet_provider: WalletProvider, checkpointer=None) -> AgentState:
    graph = build_graph(wallet_provider, checkpointer=checkpointer)
    return graph.invoke(state)


def build_checkpointer_from_env():
    load_dotenv()
    db_url = os.getenv("CHECKPOINT_DB_URL")
    if not db_url:
        return None
    if db_url.startswith("sqlite"):
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
        except ImportError as exc:
            raise RuntimeError("langgraph-checkpoint-sqlite is not installed") from exc
        path = db_url.replace("sqlite:///", "").replace("sqlite://", "")
        conn = sqlite3.connect(path or "checkpoints.db")
        return SqliteSaver(conn)
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError as exc:
        raise RuntimeError("langgraph-checkpoint-postgres is not installed") from exc
    checkpointer = PostgresSaver.from_conn_string(db_url)
    checkpointer.setup()
    return checkpointer


def run_with_config(target_url: str, wallet_provider: WalletProvider, config: Optional[AgentConfig] = None) -> AgentState:
    cfg = asdict(config) if config else None
    state = default_state(target_url, config=cfg)
    if config and config.enable_llm:
        state["llm_client"] = build_llm_bundle_from_env()
    checkpointer = None
    if config and config.enable_checkpoint:
        checkpointer = build_checkpointer_from_env() or InMemorySaver()
    graph = build_graph(wallet_provider, checkpointer=checkpointer)
    return graph.invoke(state)


def build_wallet_provider_from_config(config: AgentConfig) -> WalletProvider:
    if config.agentkit_api_key_name and config.agentkit_api_key_secret:
        return AgentKitWalletProvider.from_config(
            {
                "api_key_name": config.agentkit_api_key_name,
                "api_key_secret": config.agentkit_api_key_secret,
                "network_id": config.agentkit_network_id,
            }
        )
    raise ValueError("AgentKit credentials are required to build a wallet provider")
