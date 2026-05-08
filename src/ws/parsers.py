"""
Pure parsing helpers for Hyperliquid WS messages.

Kept in a separate module from hl_listener so they can be unit-tested
without needing the `websockets` library installed.
"""
from __future__ import annotations
from typing import Optional


def detect_liquidation_field(tr: dict) -> Optional[dict]:
    """Return a dict describing the liquidation if this trade is one, else None.

    Tries multiple known shapes for the `liquidation` field on a trade message.
    Hyperliquid docs/SDKs disagree on the exact shape, so we accept several:

      A) tr["liquidation"] is a dict — e.g. {"liquidatedUser": "0x...", "method": "market"}
      B) tr["liquidation"] is True/False — some relays
      C) tr["liquidation"] is a string method — fallback

    Returns:
      None: this is not a liquidation
      dict: liquidation (may be empty if no metadata available)
    """
    if not isinstance(tr, dict):
        return None
    liq = tr.get("liquidation")
    if liq is None:
        return None
    if isinstance(liq, dict):
        return liq
    if isinstance(liq, bool):
        return {} if liq else None
    if isinstance(liq, str):
        return {"method": liq}
    return None
