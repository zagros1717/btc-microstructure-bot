"""ws subpackage. Imports kept lazy so tests can import market_state alone."""
from .market_state import MarketState

# Lazy access to HyperliquidListener (needs `websockets` installed)
def __getattr__(name: str):
    if name == "HyperliquidListener":
        from .hl_listener import HyperliquidListener
        return HyperliquidListener
    raise AttributeError(name)

__all__ = ["HyperliquidListener", "MarketState"]
