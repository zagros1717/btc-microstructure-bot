"""execution subpackage. Lazy imports so individual modules can be tested."""

def __getattr__(name: str):
    if name == "PaperExecutor":
        from .paper_executor import PaperExecutor
        return PaperExecutor
    if name == "LiveExecutor":
        from .live_executor import LiveExecutor
        return LiveExecutor
    if name == "TradeManager":
        from .manager import TradeManager
        return TradeManager
    raise AttributeError(name)

__all__ = ["PaperExecutor", "LiveExecutor", "TradeManager"]
