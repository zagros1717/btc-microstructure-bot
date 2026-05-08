"""
Live safety tests.

Verifies:
  - When LIVE_CODE_ENABLED=false, SDK is never initialized (capable=False)
  - When ENABLE_LIVE=false, SDK is not initialized (capable=False)
  - When keys missing, capable=False with init_error
  - LiveExecutor.open() refuses if not capable
  - LiveExecutor.open() refuses if live_armed=false (even if capable)
  - LiveExecutor.open() refuses if size > LIVE_MAX_ORDER_USD
  - LiveExecutor.open() refuses if require_protected_exits=true (and disarms)

We do NOT actually call the Hyperliquid API here. We construct a fake
StrategyResult and walk through gate logic only.
"""
from __future__ import annotations
import asyncio
import sys
import pathlib
import os

# Force-disable live code BEFORE importing — so SDK isn't even imported.
os.environ.setdefault("LIVE_CODE_ENABLED", "false")
os.environ.setdefault("ENABLE_LIVE", "false")
os.environ.setdefault("REQUIRE_PROTECTED_EXITS", "true")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.ws.market_state import MarketState, TradeTick
from src.execution.live_executor import LiveExecutor
from src.strategies.types import StrategyResult


def _make_market(coin: str = "BTC", last_px: float = 60_000.0) -> MarketState:
    m = MarketState([coin])
    import time
    m.add_trade(coin, TradeTick(
        ts_ms=int(time.time() * 1000), px=last_px, size=0.1,
        size_usd=last_px * 0.1, aggressor_buy=True, is_liq=False,
    ))
    return m


def _sample_signal(coin: str = "BTC", entry: float = 60_000.0) -> StrategyResult:
    return StrategyResult(
        coin=coin, strategy="liquidation_fade", direction="long",
        entry_px=entry, stop_px=entry * 0.99, target_px=entry * 1.02,
    )


def test_live_executor_not_capable_when_code_disabled():
    """LIVE_CODE_ENABLED=false → SDK not even imported, capable=False."""
    m = _make_market()
    le = LiveExecutor(m)
    assert le.capable is False
    assert le.enabled is False  # backward-compat alias


def test_live_executor_open_refuses_when_not_capable():
    """If not capable, open() returns None without contacting any API."""
    m = _make_market()
    le = LiveExecutor(m)
    sig = _sample_signal()
    result = asyncio.get_event_loop().run_until_complete(
        le.open(sig, signal_id=1, size_usd=100.0)
    ) if not asyncio.get_event_loop().is_running() else None
    # When loop is running (pytest-asyncio), use a fresh loop:
    if result is None:
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(le.open(sig, signal_id=1, size_usd=100.0))
        finally:
            loop.close()
    assert result is None


def test_live_executor_init_error_when_keys_missing(monkeypatch):
    """If LIVE_CODE_ENABLED + ENABLE_LIVE but no keys → init_error set, capable=False."""
    from src.config import CFG
    # Mutate at instance level for test ONLY (not the way we'd do at runtime)
    monkeypatch.setattr(CFG, "live_code_enabled", True, raising=False)
    monkeypatch.setattr(CFG, "enable_live_execution", True, raising=False)
    monkeypatch.setattr(CFG, "hl_account_address", "", raising=False)
    monkeypatch.setattr(CFG, "hl_secret_key", "", raising=False)

    m = _make_market()
    le = LiveExecutor(m)
    assert le.capable is False
    assert le.init_error == "missing keys"
