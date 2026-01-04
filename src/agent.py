from src.graph import build_graph
from src.service_bootstrap import ensure_services_started
from src.utils import DummyWalletProvider


ensure_services_started()
graph = build_graph(DummyWalletProvider())
