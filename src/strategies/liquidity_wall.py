"""
Liquidity Wall Reversal — BTC ONLY.
====================================

Idea:
  Real, persistent liquidity walls in the order book (large limit orders that
  STAY for tens of seconds despite price action) tend to absorb price impact.
  When price reaches such a wall, gets hit with significant traded volume,
  and aggressor flow flips, the rebound is a high-probability fade.

  Distinguishing REAL walls from spoofs is the hard part. Spoofs:
    - appear briefly (< wall_track_seconds)
    - or shrink/cancel right before price reaches them.

State machine per wall:
  tracking -> confirmed -> (consumed | vanished | expired | fired)

  tracking:  level just detected; needs to survive wall_track_seconds
  confirmed: stayed stable for wall_track_seconds (shrunk <= max_shrink_pct)
  vanished:  shrunk > max_shrink_pct or disappeared
  expired:   confirmed but price moved past it without absorption
  fired:     a signal was emitted using this wall

Lifecycle is persisted to wall_events table for offline analysis.
Direction:
  - bid-side wall hit + buyer flip -> LONG (fade the dip)
  - ask-side wall hit + seller flip -> SHORT (fade the rip)

We fire ONLY ONE signal per wall id (deduped). Per-side cooldown 90s.
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .types import StrategyResult
from .math_utils import atr_from_candles
from ..config import CFG
from ..ws.market_state import MarketState, L2Snapshot

log = logging.getLogger(__name__)


@dataclass
class TrackedWall:
    wall_id: str
    side: str                # 'bid' | 'ask'
    px: float
    size_usd_initial: float
    size_usd_current: float
    size_usd_min: float
    detected_ms: int
    confirmed_ms: Optional[int] = None
    consumed_ms: Optional[int] = None
    state: str = "tracking"  # tracking|confirmed|vanished|expired|fired
    traded_into_usd: float = 0.0
    last_seen_ms: int = 0
    db_id: Optional[int] = None  # row id in wall_events


class LiquidityWallStrategy:
    name = "liquidity_wall"
    COIN = "BTC"

    PX_ROUND_DOLLARS = 5.0
    BROKEN_ATR_MULT = 0.6
    POST_HIT_WINDOW_S = 8.0
    SIGNAL_GAP_S = 90

    def __init__(self, market: MarketState):
        self.market = market
        self.walls: Dict[str, TrackedWall] = {}
        self._last_fire_ms: Dict[str, int] = {"bid": 0, "ask": 0}
        # Pending DB ops queue: wall_id -> "create" | "update".
        # We can't await inside on_l2 (called sync from listener), so we
        # accumulate dirty walls and flush them periodically. The orchestrator
        # awaits flush_persistence() each tick.
        self._dirty: Dict[str, str] = {}

    def _threshold_check(self, level_usd: float, avg_nearby: float) -> bool:
        return (
            level_usd >= CFG.wall_min_size_usd
            and level_usd >= avg_nearby * CFG.wall_size_multiple
        )

    def on_l2(self, snap: L2Snapshot) -> None:
        """Called every time a new L2 snapshot arrives. Synchronous."""
        ts = snap.ts_ms
        if not snap.bids or not snap.asks:
            return

        mid = (snap.bids[0].px + snap.asks[0].px) / 2.0
        all_sizes_usd = (
            [b.size_usd for b in snap.bids[:20]]
            + [a.size_usd for a in snap.asks[:20]]
        )
        if not all_sizes_usd:
            return
        avg_nearby = sum(all_sizes_usd) / len(all_sizes_usd)
        if avg_nearby <= 0:
            return

        present: Dict[str, tuple] = {}
        for lvl in snap.bids[:20]:
            if self._threshold_check(lvl.size_usd, avg_nearby) and lvl.px < mid:
                wid = self._wall_id("bid", lvl.px)
                present[wid] = ("bid", lvl.px, lvl.size_usd)
        for lvl in snap.asks[:20]:
            if self._threshold_check(lvl.size_usd, avg_nearby) and lvl.px > mid:
                wid = self._wall_id("ask", lvl.px)
                present[wid] = ("ask", lvl.px, lvl.size_usd)

        # Update existing walls
        for wid, w in list(self.walls.items()):
            if w.state in ("vanished", "expired", "fired"):
                continue
            if wid in present:
                _, _, sz = present[wid]
                w.size_usd_current = sz
                w.size_usd_min = min(w.size_usd_min, sz)
                w.last_seen_ms = ts
                if (w.state == "tracking"
                        and ts - w.detected_ms >= CFG.wall_track_seconds * 1000):
                    shrink_pct = (1 - w.size_usd_min / w.size_usd_initial) * 100
                    if shrink_pct <= CFG.wall_max_shrink_pct:
                        w.state = "confirmed"
                        w.confirmed_ms = ts
                        self._dirty[wid] = "update"
                        log.info(
                            "wall confirmed %s @ $%.2f size=$%.0fk",
                            w.side, w.px, w.size_usd_initial / 1000,
                        )
            else:
                shrink_pct = (1 - w.size_usd_current / w.size_usd_initial) * 100
                if shrink_pct > CFG.wall_max_shrink_pct:
                    w.state = "vanished"
                    self._dirty[wid] = "update"
                else:
                    if ts - w.last_seen_ms > 5000:
                        w.state = "vanished"
                        self._dirty[wid] = "update"

        # New walls
        for wid, (side, px, sz) in present.items():
            if wid not in self.walls:
                self.walls[wid] = TrackedWall(
                    wall_id=wid, side=side, px=px,
                    size_usd_initial=sz, size_usd_current=sz, size_usd_min=sz,
                    detected_ms=ts, last_seen_ms=ts,
                )
                self._dirty[wid] = "create"

        atr = self._atr() or 0.0
        for w in self.walls.values():
            if w.state != "confirmed":
                continue
            if w.side == "bid" and atr > 0 and mid < w.px - atr * self.BROKEN_ATR_MULT:
                w.state = "expired"
                self._dirty[w.wall_id] = "update"
            elif w.side == "ask" and atr > 0 and mid > w.px + atr * self.BROKEN_ATR_MULT:
                w.state = "expired"
                self._dirty[w.wall_id] = "update"

        # GC very-old terminal walls
        cutoff = ts - 30 * 60 * 1000
        for wid in list(self.walls.keys()):
            w = self.walls[wid]
            if w.last_seen_ms < cutoff and w.state in ("vanished", "expired", "fired"):
                del self.walls[wid]

    def _wall_id(self, side: str, px: float) -> str:
        rounded = round(px / self.PX_ROUND_DOLLARS) * self.PX_ROUND_DOLLARS
        return f"{side}:{rounded:.2f}"

    def _atr(self) -> Optional[float]:
        candles = self.market.get_closed_candles(self.COIN)
        return atr_from_candles(candles, period=14)

    async def flush_persistence(self) -> None:
        """Persist dirty wall states to DB. Best-effort.

        DB imports are lazy so tests can import this module without sqlalchemy.
        """
        if not self._dirty:
            return
        snapshot = list(self._dirty.items())
        self._dirty.clear()
        try:
            # Lazy import: tests that exercise on_l2/evaluate without DB don't
            # need sqlalchemy installed.
            from ..db import session_scope, WallEvent
            async with session_scope() as s:
                for wid, op in snapshot:
                    w = self.walls.get(wid)
                    if w is None:
                        continue
                    if op == "create" and w.db_id is None:
                        ev = WallEvent(
                            coin=self.COIN, wall_id=w.wall_id, side=w.side, px=w.px,
                            size_usd_initial=w.size_usd_initial,
                            size_usd_min=w.size_usd_min,
                            detected_ms=w.detected_ms, final_state=w.state,
                        )
                        s.add(ev)
                        await s.flush()
                        w.db_id = ev.id
                    elif w.db_id is not None:
                        ev = await s.get(WallEvent, w.db_id)
                        if ev is not None:
                            ev.size_usd_min = w.size_usd_min
                            ev.size_usd_final = w.size_usd_current
                            ev.confirmed_ms = w.confirmed_ms
                            ev.final_state = w.state
                            if w.state in ("vanished", "expired", "consumed", "fired"):
                                ev.final_ms = int(time.time() * 1000)
        except Exception as e:
            log.warning("wall persistence flush failed: %s", e)

    def evaluate(self, now_ms: Optional[int] = None) -> StrategyResult:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        result = StrategyResult(coin=self.COIN, strategy=self.name)

        snap = self.market.l2_latest.get(self.COIN)
        if snap is None or not snap.bids or not snap.asks:
            result.reject_reason = "no L2 yet"
            return result

        last_px = self.market.latest_price(self.COIN)
        if last_px is None:
            result.reject_reason = "no price"
            return result

        atr = self._atr()
        if atr is None or atr <= 0:
            result.reject_reason = "insufficient candle history"
            return result

        candidates: List[TrackedWall] = []
        prox_dollars = max(last_px * CFG.wall_proximity_bps / 10_000.0, 1.0)
        for w in self.walls.values():
            if w.state != "confirmed":
                continue
            if abs(last_px - w.px) > prox_dollars * 4:
                continue
            candidates.append(w)
        if not candidates:
            result.reject_reason = "no confirmed wall near price"
            return result

        w = min(candidates, key=lambda x: abs(last_px - x.px))

        if (now_ms - self._last_fire_ms[w.side]) / 1000.0 < self.SIGNAL_GAP_S:
            result.reject_reason = f"side cooldown ({w.side})"
            return result

        if abs(last_px - w.px) > prox_dollars:
            result.reject_reason = (
                f"price not at wall (dist=${abs(last_px - w.px):.2f}, "
                f"prox=${prox_dollars:.2f})"
            )
            return result

        recent = self.market.trades_in_window(
            self.COIN, int(self.POST_HIT_WINDOW_S * 1000), now_ms,
        )
        traded_at_level = sum(
            t.size_usd for t in recent
            if abs(t.px - w.px) <= prox_dollars
        )
        if traded_at_level < CFG.wall_min_traded_into:
            result.reject_reason = (
                f"insufficient hit volume (${traded_at_level:,.0f} of "
                f"${CFG.wall_min_traded_into:,.0f})"
            )
            return result

        shrink_pct = (1 - w.size_usd_current / w.size_usd_initial) * 100
        if shrink_pct < 5:
            result.reject_reason = f"wall barely shrunk ({shrink_pct:.1f}%)"
            return result
        if shrink_pct > 90:
            result.reject_reason = (
                f"wall consumed too completely ({shrink_pct:.1f}% gone — broken)"
            )
            return result

        flip_win_ms = int(CFG.wall_aggressor_flip_window_s * 1000)
        ratio = self.market.aggressor_buy_ratio(self.COIN, flip_win_ms, now_ms)
        if ratio is None:
            result.reject_reason = "no flow data"
            return result

        if w.side == "bid":
            our_dir = "long"
            if ratio < 0.55:
                result.reject_reason = f"flow not flipped to buyers (ratio={ratio:.2f})"
                return result
        else:
            our_dir = "short"
            if ratio > 0.45:
                result.reject_reason = f"flow not flipped to sellers (ratio={ratio:.2f})"
                return result

        entry = last_px
        stop_buffer = entry * CFG.wall_stop_buffer_bps / 10_000.0
        if our_dir == "long":
            stop = w.px - stop_buffer
            target = entry + atr * CFG.wall_target_atr_mult
            if not (stop < entry < target):
                result.reject_reason = "invalid geometry (long)"
                return result
        else:
            stop = w.px + stop_buffer
            target = entry - atr * CFG.wall_target_atr_mult
            if not (target < entry < stop):
                result.reject_reason = "invalid geometry (short)"
                return result

        risk = abs(entry - stop)
        reward = abs(target - entry)
        if risk <= 0 or reward / risk < 1.0:
            result.reject_reason = f"poor RR ({reward/max(risk,1e-9):.2f})"
            return result

        w.state = "fired"
        self._dirty[w.wall_id] = "update"
        self._last_fire_ms[w.side] = now_ms

        result.direction = our_dir
        result.entry_px = float(entry)
        result.stop_px = float(stop)
        result.target_px = float(target)
        result.meta = {
            "wall_id": w.wall_id,
            "wall_db_id": w.db_id,
            "wall_side": w.side,
            "wall_px": float(w.px),
            "wall_initial_usd": float(w.size_usd_initial),
            "wall_current_usd": float(w.size_usd_current),
            "shrink_pct": float(shrink_pct),
            "traded_at_level_usd": float(traded_at_level),
            "flow_ratio": float(ratio),
            "atr_1m": float(atr),
        }
        return result
