"""
Position sizing.

Risk-based: size such that hitting the stop loses (risk_pct × equity).
Always capped by hardcoded_max_position_usd.
"""
from __future__ import annotations
from ..config import CFG


def compute_size_usd(equity_usd: float, entry_px: float, stop_px: float) -> float:
    """
    Returns USD notional for the position.
    """
    if equity_usd <= 0 or entry_px <= 0 or stop_px <= 0:
        return 0.0
    risk_dollars = equity_usd * (CFG.risk_per_trade_pct / 100.0)
    risk_per_unit_pct = abs(entry_px - stop_px) / entry_px
    if risk_per_unit_pct <= 0:
        return 0.0
    notional = risk_dollars / risk_per_unit_pct
    notional = min(notional, CFG.hardcoded_max_position_usd)
    notional = min(notional, equity_usd * CFG.max_leverage)
    notional = max(notional, 0.0)
    return float(notional)
