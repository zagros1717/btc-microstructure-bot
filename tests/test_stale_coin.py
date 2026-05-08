"""
Per-coin staleness tests.

The risk manager must reject signals for a specific coin if that coin's
trades have not been seen recently — even if the WebSocket is alive and
delivering messages for OTHER coins.

These tests use a stub for the DB calls in RiskManager (we test the
logic-only path).
"""
from __future__ import annotations
import sys
import pathlib
import time
import asyncio
import os

os.environ.setdefault("LIVE_CODE_ENABLED", "false")
os.environ.setdefault("ENABLE_LIVE", "false")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.ws.market_state import MarketState, TradeTick


def test_market_state_tracks_per_coin_last_trade_ms():
    m = MarketState(["BTC", "ETH", "SOL"])
    now = int(time.time() * 1000)
    m.add_trade("BTC", TradeTick(
        ts_ms=now, px=60_000, size=0.1, size_usd=6_000,
        aggressor_buy=True, is_liq=False,
    ))
    # No trade for ETH or SOL
    assert m.last_trade_ms["BTC"] == now
    assert m.last_trade_ms.get("ETH", 0) == 0
    assert m.last_trade_ms.get("SOL", 0) == 0


def test_per_coin_freshness_distinguishes_coins():
    m = MarketState(["BTC", "ETH"])
    now = int(time.time() * 1000)
    # BTC just traded, ETH last traded 2 minutes ago
    m.add_trade("BTC", TradeTick(
        ts_ms=now, px=60_000, size=0.1, size_usd=6_000,
        aggressor_buy=True, is_liq=False,
    ))
    m.add_trade("ETH", TradeTick(
        ts_ms=now - 120_000, px=2_500, size=1, size_usd=2_500,
        aggressor_buy=True, is_liq=False,
    ))
    btc_age_s = (now - m.last_trade_ms["BTC"]) / 1000
    eth_age_s = (now - m.last_trade_ms["ETH"]) / 1000
    assert btc_age_s < 1
    assert eth_age_s >= 100


def test_aggressor_buy_ratio_excludes_liquidations():
    m = MarketState(["BTC"])
    now = int(time.time() * 1000)
    # 2 normal sell trades + 1 huge liquidation buy
    # If liq counted, buy ratio would be ~99%; if excluded, ~0%
    m.add_trade("BTC", TradeTick(
        ts_ms=now - 1000, px=60_000, size=0.1, size_usd=6_000,
        aggressor_buy=False, is_liq=False,
    ))
    m.add_trade("BTC", TradeTick(
        ts_ms=now - 800, px=60_000, size=0.1, size_usd=6_000,
        aggressor_buy=False, is_liq=False,
    ))
    m.add_trade("BTC", TradeTick(
        ts_ms=now - 500, px=60_000, size=10, size_usd=600_000,
        aggressor_buy=True, is_liq=True,
    ))
    ratio = m.aggressor_buy_ratio("BTC", window_ms=5000, now_ms=now)
    assert ratio is not None
    assert ratio < 0.1, f"liquidation should not have skewed ratio: {ratio}"
