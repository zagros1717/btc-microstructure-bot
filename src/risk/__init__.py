"""risk subpackage. Imports kept lazy so sizing alone can be used in tests."""
from .sizing import compute_size_usd

def __getattr__(name: str):
    if name == "RiskManager":
        from .limits import RiskManager
        return RiskManager
    raise AttributeError(name)

__all__ = ["RiskManager", "compute_size_usd"]
