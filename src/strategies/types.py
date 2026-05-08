"""Common types for strategies."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StrategyResult:
    """If signal is None, the strategy chose not to fire (reject_reason explains why).
    Otherwise the signal fields describe a proposed trade."""
    coin: str
    strategy: str
    direction: Optional[str] = None  # 'long' | 'short'
    entry_px: Optional[float] = None
    stop_px: Optional[float] = None
    target_px: Optional[float] = None
    size_usd: Optional[float] = None
    meta: dict = field(default_factory=dict)
    reject_reason: Optional[str] = None

    @property
    def has_signal(self) -> bool:
        return self.direction is not None and self.entry_px is not None


@dataclass
class SignalReject:
    coin: str
    strategy: str
    reason: str
