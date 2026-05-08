"""Shared types between executors."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class OpenPosition:
    id: int
    kind: str          # 'paper' | 'live'
    coin: str
    strategy: str
    direction: str
    size_usd: float
    entry_fill_px: float
    stop_px: float
    target_px: float
    opened_ms: int
    fees_usd: float
