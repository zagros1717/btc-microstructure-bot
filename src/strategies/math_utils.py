"""Pure math/feature helpers. No I/O."""
from __future__ import annotations
import math
from typing import List, Optional, Tuple

Candle = Tuple[int, float, float, float, float, float]  # (open_ms, o, h, l, c, v_usd)


def atr_from_candles(candles: List[Candle], period: int = 14) -> Optional[float]:
    """Standard ATR (Wilder) on closed candles. Returns None if insufficient data."""
    if len(candles) < period + 1:
        return None
    trs: List[float] = []
    prev_close = candles[-(period + 1)][4]
    for i in range(-period, 0):
        _, _, h, l, c, _ = candles[i]
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        prev_close = c
    return sum(trs) / period if trs else None


def percentile(xs: List[float], p: float) -> Optional[float]:
    """Linear-interp percentile (p in [0,1])."""
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    p = max(0.0, min(1.0, p))
    pos = p * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac
