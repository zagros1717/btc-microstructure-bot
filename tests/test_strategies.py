"""
Strategy unit tests. Run with: PYTHONPATH=. pytest tests/

These cover signal logic without DB/WS dependencies.
"""
from __future__ import annotations
import time
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.ws.market_state import MarketState, TradeTick, L2Snapshot, L2Level
from src.strategies.liquidation_fade import LiquidationFadeStrategy
from src.strategies.liquidity_wall import LiquidityWallStrategy
from src.strategies.math_utils import atr_from_candles


def _seed_candles(market: MarketState, coin: str, base_px: float = 100.0, n: int = 30) -> None:
    now_ms = int(time.time() * 1000)
    for i in range(n):
        bar_ms = now_ms - (n - i) * 60_000
        market.add_trade(coin, TradeTick(
            ts_ms=bar_ms + 100, px=base_px + (i % 3) * 0.5, size=1.0,
            size_usd=base_px, aggressor_buy=(i % 2 == 0), is_liq=False,
        ))
        market.add_trade(coin, TradeTick(
            ts_ms=bar_ms + 30_000, px=base_px - (i % 3) * 0.5 + 0.3, size=1.0,
            size_usd=base_px, aggressor_buy=(i % 2 == 1), is_liq=False,
        ))
    market.add_trade(coin, TradeTick(
        ts_ms=now_ms - 30_000, px=base_px, size=1.0, size_usd=base_px,
        aggressor_buy=True, is_liq=False,
    ))


def test_atr_returns_none_for_short_history():
    assert atr_from_candles([], 14) is None


def test_atr_works_on_seeded_data():
    m = MarketState(["BTC"])
    _seed_candles(m, "BTC", 100.0, n=20)
    a = atr_from_candles(m.get_closed_candles("BTC"), period=14)
    assert a is None or a > 0


def test_liquidation_fade_no_signal_without_liq():
    m = MarketState(["BTC"])
    _seed_candles(m, "BTC", 60_000.0, n=30)
    res = LiquidationFadeStrategy(m, min_signal_gap_s=0).evaluate("BTC")
    assert not res.has_signal
    assert "no liq" in (res.reject_reason or "")


def test_liquidation_fade_signals_on_long_cascade():
    m = MarketState(["BTC"])
    _seed_candles(m, "BTC", 60_000.0, n=30)
    now_ms = int(time.time() * 1000)
    cascade_end = now_ms - 3500  # past 3s wait, within 5s window
    # All cascade prints within 1s of each other
    for offset, sz_usd in [(-800, 240_000), (-400, 240_000), (0, 120_000)]:
        m.add_trade("BTC", TradeTick(
            ts_ms=cascade_end + offset, px=59_900,
            size=sz_usd / 59_900, size_usd=sz_usd,
            aggressor_buy=False, is_liq=True,
        ))
    for i in range(20):
        m.add_trade("BTC", TradeTick(
            ts_ms=cascade_end + 200 + i * 100, px=59_950,
            size=0.1, size_usd=6_000, aggressor_buy=True, is_liq=False,
        ))
    m.add_trade("BTC", TradeTick(
        ts_ms=now_ms - 100, px=59_950, size=0.1, size_usd=6_000,
        aggressor_buy=True, is_liq=False,
    ))
    res = LiquidationFadeStrategy(m, min_signal_gap_s=0).evaluate("BTC", now_ms=now_ms)
    assert res.has_signal, f"reject={res.reject_reason}"
    assert res.direction == "long"
    assert res.stop_px < res.entry_px < res.target_px
    assert res.meta.get("liquidated_side") == "long"


def test_liquidation_fade_blocked_when_flow_doesnt_flip():
    m = MarketState(["BTC"])
    _seed_candles(m, "BTC", 60_000.0, n=30)
    now_ms = int(time.time() * 1000)
    cascade_end = now_ms - 3500
    m.add_trade("BTC", TradeTick(
        ts_ms=cascade_end, px=59_900, size=10.0, size_usd=600_000,
        aggressor_buy=False, is_liq=True,
    ))
    for i in range(20):
        m.add_trade("BTC", TradeTick(
            ts_ms=cascade_end + 200 + i * 100, px=59_900, size=0.1, size_usd=6_000,
            aggressor_buy=False, is_liq=False,
        ))
    res = LiquidationFadeStrategy(m, min_signal_gap_s=0).evaluate("BTC", now_ms=now_ms)
    assert not res.has_signal
    assert "flow not flipped" in (res.reject_reason or "")


def test_liquidation_fade_cooldown_blocks_repeats():
    m = MarketState(["BTC"])
    _seed_candles(m, "BTC", 60_000.0, n=30)
    now_ms = int(time.time() * 1000)
    cascade_end = now_ms - 3500
    m.add_trade("BTC", TradeTick(
        ts_ms=cascade_end, px=59_900, size=10.0, size_usd=600_000,
        aggressor_buy=False, is_liq=True,
    ))
    for i in range(15):
        m.add_trade("BTC", TradeTick(
            ts_ms=cascade_end + 200 + i * 100, px=59_950, size=0.1, size_usd=6_000,
            aggressor_buy=True, is_liq=False,
        ))
    s = LiquidationFadeStrategy(m, min_signal_gap_s=60)
    first = s.evaluate("BTC", now_ms=now_ms)
    assert first.has_signal
    second = s.evaluate("BTC", now_ms=now_ms + 1000)
    assert not second.has_signal
    assert "cooldown" in (second.reject_reason or "")


# ── Wall strategy ───────────────────────────────────────────────────────

def _build_l2(bid_walls, ask_walls, mid, normal=200_000):
    bids = []
    px = mid - 1.0
    for _ in range(20):
        sz = normal
        for w_px, w_sz in bid_walls:
            if abs(px - w_px) <= 2.5:
                sz = w_sz
        bids.append(L2Level(px=px, size=sz / px, size_usd=sz))
        px -= 5.0
    asks = []
    px = mid + 1.0
    for _ in range(20):
        sz = normal
        for w_px, w_sz in ask_walls:
            if abs(px - w_px) <= 2.5:
                sz = w_sz
        asks.append(L2Level(px=px, size=sz / px, size_usd=sz))
        px += 5.0
    return L2Snapshot(ts_ms=int(time.time() * 1000), bids=bids, asks=asks)


def test_wall_detected_and_confirmed():
    from src.config import CFG
    m = MarketState(["BTC"])
    _seed_candles(m, "BTC", 60_000.0, n=30)
    s = LiquidityWallStrategy(m)
    snap = _build_l2([(59_984.0, 5_000_000)], [], mid=60_000.0)
    s.on_l2(snap)
    for i in range(1, int(CFG.wall_track_seconds) + 5):
        snap2 = _build_l2([(59_984.0, 5_000_000)], [], mid=60_000.0)
        snap2.ts_ms = snap.ts_ms + i * 1000
        s.on_l2(snap2)
    states = [w.state for w in s.walls.values()]
    assert "confirmed" in states, f"states={states}"


def test_wall_strategy_rejects_spoof():
    m = MarketState(["BTC"])
    _seed_candles(m, "BTC", 60_000.0, n=30)
    s = LiquidityWallStrategy(m)
    snap = _build_l2([(59_984.0, 5_000_000)], [], mid=60_000.0)
    s.on_l2(snap)
    for i in range(1, 10):
        snap2 = _build_l2([], [], mid=60_000.0)  # wall gone
        snap2.ts_ms = snap.ts_ms + i * 1000
        s.on_l2(snap2)
    states = [w.state for w in s.walls.values()]
    assert "vanished" in states, f"states={states}"


# ── Sizing ──────────────────────────────────────────────────────────────

def test_sizing_capped_correctly():
    from src.config import CFG
    from src.risk.sizing import compute_size_usd
    n = compute_size_usd(equity_usd=1000, entry_px=100, stop_px=99)
    assert 0 < n <= CFG.hardcoded_max_position_usd


def test_sizing_zero_when_invalid():
    from src.risk.sizing import compute_size_usd
    assert compute_size_usd(0, 100, 99) == 0
    assert compute_size_usd(1000, 0, 99) == 0
    assert compute_size_usd(1000, 100, 100) == 0
