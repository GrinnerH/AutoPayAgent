from langchain_core.tools import tool

from src.utils import default_service_registry


@tool
def list_services():
    """Return available x402-capable services."""
    return default_service_registry()
