from src.graph import build_graph
from src.llm import build_llm_bundle_from_env
from src.nodes import set_llm_bundle
from src.service_bootstrap import ensure_services_started
from src.utils import EthAccountWalletProvider


ensure_services_started()
llm_bundle = build_llm_bundle_from_env()
if llm_bundle is None:
    raise RuntimeError("LLM is required for langgraph dev. Set OPENAI_API_KEY, MODEL_NAME, BASE_URL.")
set_llm_bundle(llm_bundle)
graph = build_graph(EthAccountWalletProvider.from_env()).with_config({"recursion_limit": 100})
