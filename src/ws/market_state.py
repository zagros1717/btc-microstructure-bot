"""
In-memory market state shared between WS listener and strategies.

Why in-memory: trades arrive at high rate (10s-100s/sec on busy coins).
Writing every trade to Postgres is wasteful. We keep a rolling buffer in
memory; only liquidation-flagged trades and wall events are persisted.

All access must be from the asyncio event loop (single thread). No locks.
"""
from __future__ import annotations
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple


@dataclass
class TradeTick:
    ts_ms: int
    px: float
    size: float           # base units
    size_usd: float
    aggressor_buy: bool   # True if 'B' (taker bought = aggressive buy)
    is_liq: bool
    liq_user: Optional[str] = None
    liq_method: Optional[str] = None


@dataclass
class L2Level:
    px: float
    size: float       # base units
    size_usd: float


@dataclass
class L2Snapshot:
    ts_ms: int
    bids: List[L2Level] = field(default_factory=list)  # sorted high->low
    asks: List[L2Level] = field(default_factory=list)  # sorted low->high

    @property
    def mid(self) -> Optional[float]:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0].px + self.asks[0].px) / 2.0


class MarketState:
    """Per-coin rolling market state."""

    # Buffer sizes are deliberately bounded to prevent memory growth.
    TRADE_BUFFER = 5000        # ~5-10 min on busy coins
    L2_BUFFER = 200            # snapshots
    CANDLE_BUFFER_1M = 240     # 4 hours of 1m bars

    def __init__(self, coins: List[str]):
        self.coins = coins
        # coin -> deque of TradeTick (newest at right)
        self.trades: Dict[str, Deque[TradeTick]] = {c: deque(maxlen=self.TRADE_BUFFER) for c in coins}
        # coin -> latest L2 + a tiny history for stability checks
        self.l2_latest: Dict[str, Optional[L2Snapshot]] = {c: None for c in coins}
        self.l2_history: Dict[str, Deque[L2Snapshot]] = {c: deque(maxlen=self.L2_BUFFER) for c in coins}
        # coin -> 1m candles built locally from trades for ATR, etc.
        # Stored as (open_ms, o, h, l, c, v_usd)
        self.candles_1m: Dict[str, Deque[Tuple[int, float, float, float, float, float]]] = {
            c: deque(maxlen=self.CANDLE_BUFFER_1M) for c in coins
        }
        self._cur_bar: Dict[str, Optional[List]] = {c: None for c in coins}
        # Last seen trade ts (for staleness detection)
        self.last_trade_ms: Dict[str, int] = {c: 0 for c in coins}

    # ── trade ingestion ─────────────────────────────────────────────────
    def add_trade(self, coin: str, t: TradeTick) -> None:
        if coin not in self.trades:
            return
        self.trades[coin].append(t)
        self.last_trade_ms[coin] = t.ts_ms
        self._update_candle(coin, t)

    def _update_candle(self, coin: str, t: TradeTick) -> None:
        bar_ms = (t.ts_ms // 60_000) * 60_000
        cur = self._cur_bar[coin]
        if cur is None or cur[0] != bar_ms:
            # close previous
            if cur is not None:
                self.candles_1m[coin].append(tuple(cur))
            self._cur_bar[coin] = [bar_ms, t.px, t.px, t.px, t.px, t.size_usd]
        else:
            cur[2] = max(cur[2], t.px)  # h
            cur[3] = min(cur[3], t.px)  # l
            cur[4] = t.px               # c
            cur[5] += t.size_usd        # v_usd

    def get_closed_candles(self, coin: str) -> List[Tuple[int, float, float, float, float, float]]:
        """Closed 1m candles. Excludes the in-progress bar."""
        return list(self.candles_1m.get(coin, []))

    # ── L2 ingestion ────────────────────────────────────────────────────
    def update_l2(self, coin: str, snap: L2Snapshot) -> None:
        if coin not in self.l2_latest:
            return
        self.l2_latest[coin] = snap
        self.l2_history[coin].append(snap)

    # ── helpers used by strategies ──────────────────────────────────────
    def trades_in_window(self, coin: str, window_ms: int, now_ms: Optional[int] = None) -> List[TradeTick]:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        cutoff = now_ms - window_ms
        return [t for t in self.trades.get(coin, ()) if t.ts_ms >= cutoff]

    def liquidations_in_window(self, coin: str, window_ms: int, now_ms: Optional[int] = None) -> List[TradeTick]:
        return [t for t in self.trades_in_window(coin, window_ms, now_ms) if t.is_liq]

    def latest_price(self, coin: str) -> Optional[float]:
        if not self.trades.get(coin):
            return None
        return self.trades[coin][-1].px

    def aggressor_buy_ratio(self, coin: str, window_ms: int, now_ms: Optional[int] = None) -> Optional[float]:
        """
        Returns fraction of usd-volume that was aggressive-buy in window.
        None if no trades in window. Excludes liquidation trades because their
        aggressor side is forced by the liquidator and adds noise to flow signals.
        """
        ts = self.trades_in_window(coin, window_ms, now_ms)
        ts = [t for t in ts if not t.is_liq]
        if not ts:
            return None
        buy = sum(t.size_usd for t in ts if t.aggressor_buy)
        total = sum(t.size_usd for t in ts)
        if total <= 0:
            return None
        return buy / total
