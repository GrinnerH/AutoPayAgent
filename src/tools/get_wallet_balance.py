from langchain.tools import tool


@tool
def get_wallet_balance() -> float:
    """Return the current wallet balance (stubbed for demo)."""
    return 0.0
