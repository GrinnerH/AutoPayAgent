from dataclasses import dataclass


@dataclass
class AgentConfig:
    budget_limit: float = 1.0
    auto_approve_threshold: float = 0.1
    hitl_threshold: float = 2.0
    hard_limit: float = 10.0
    enable_llm: bool = False
    thread_id: str = ""
    max_payment_retries: int = 3
    max_network_retries: int = 3
    preferred_network: str = "base"
    preferred_asset: str = "usdc"
    preferred_scheme: str = "exact"
    enable_checkpoint: bool = False
    agentkit_api_key_name: str = ""
    agentkit_api_key_secret: str = ""
    agentkit_network_id: str = "base-mainnet"
    whitelist_domains: tuple = ()
    whitelist_services: tuple = ()
    payee_blacklist: tuple = ()
