from langchain_core.tools import tool


@tool
def get_payee_reputation(payee: str) -> str:
    """Return a reputation label for a payee address (stubbed)."""
    if not payee:
        return "unknown"
    return "ok"
