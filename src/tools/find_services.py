from typing import Dict, List, Optional

from langchain.tools import tool

from utils import default_service_registry


@tool
def find_services(query: str, service_type: Optional[str] = None) -> List[Dict[str, object]]:
    """Find services matching a query or service type."""
    services = default_service_registry()
    if not query and not service_type:
        return services
    query_l = (query or "").lower()
    service_l = (service_type or "").lower()
    results = []
    for svc in services:
        name = str(svc.get("name", "")).lower()
        if query_l and query_l in name:
            results.append(svc)
            continue
        if service_l and service_l in name:
            results.append(svc)
    return results or services
