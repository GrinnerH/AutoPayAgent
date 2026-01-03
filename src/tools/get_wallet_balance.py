import os

from langchain_core.tools import tool


@tool
def get_wallet_balance() -> float:
    """Return the current wallet balance (stubbed for demo)."""
    raw = os.getenv("MOCK_WALLET_BALANCE", "0")
    try:
        return float(raw)
    except ValueError:
        return 0.0
