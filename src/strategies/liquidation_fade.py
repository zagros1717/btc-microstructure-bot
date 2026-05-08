"""
Liquidation Fade strategy.
=========================

Idea:
  When a cluster of liquidation-flagged trades hits within a short window,
  it represents forced, price-insensitive market orders. Price typically
  overshoots; we fade IN THE OPPOSITE DIRECTION of the liquidated side,
  AFTER waiting for the cascade to end (aggressor flow flips back).

Detection:
  Sum |size_usd| of liquidation trades in the last N seconds (default 5s),
  separated by liquidated_side (long vs short).
  If the dominant side's total exceeds the per-coin threshold, we declare
  a cascade.

Threshold (USD, configurable):
  - BTC:        500k
  - ETH:        250k
  - other alts: 150k

Direction:
  - Longs liquidated (forced sells) -> price overshoots DOWN -> we go LONG.
  - Shorts liquidated (forced buys) -> price overshoots UP   -> we go SHORT.

Confirmation:
  After the cascade, wait at least `liq_post_cascade_wait_s` seconds. Then,
  in the most recent half of that window, check that aggressor flow has
  flipped IN OUR DIRECTION (i.e., for a long fade, recent aggressor-buy
  ratio > 0.55). If not, abort.
  Cap entry latency at `liq_max_post_cascade_age_s` so we don't enter a
  stale signal.

Risk:
  - Entry: latest price.
  - Stop:  entry -/+ (ATR_1m * liq_stop_atr_mult). Default 1.5×.
  - Target: entry +/- (ATR_1m * liq_target_atr_mult). Default 2.0×.
  - Time stop: liq_time_stop_seconds after entry.

Deduplication:
  Each strategy instance keeps the timestamp of the last fired signal per
  coin and refuses to fire again within `min_signal_gap_s` (60s default).
"""
from __future__ import annotations
import logging
import time
from collections import defaultdict
from typing import Dict, Optional

from .types import StrategyResult
from .math_utils import atr_from_candles
from ..config import CFG
from ..ws.market_state import MarketState

log = logging.getLogger(__name__)


class LiquidationFadeStrategy:
    name = "liquidation_fade"

    def __init__(self, market: MarketState, min_signal_gap_s: int = 60):
        self.market = market
        self.min_signal_gap_s = min_signal_gap_s
        self._last_signal_ms: Dict[str, int] = defaultdict(int)
        # Track recent cascades to avoid double-counting
        self._last_cascade_end_ms: Dict[str, int] = defaultdict(int)

    def _threshold_for(self, coin: str) -> float:
        if coin == "BTC":
            return CFG.liq_min_cascade_btc_usd
        if coin == "ETH":
            return CFG.liq_min_cascade_eth_usd
        return CFG.liq_min_cascade_alt_usd

    def evaluate(self, coin: str, now_ms: Optional[int] = None) -> StrategyResult:
        """
        Called periodically (e.g. each tick or every 250ms by the orchestrator).
        Returns a StrategyResult; check `has_signal` and `reject_reason`.
        """
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        result = StrategyResult(coin=coin, strategy=self.name)

        # Cooldown between fires per coin
        gap_ms = now_ms - self._last_signal_ms[coin]
        if gap_ms < self.min_signal_gap_s * 1000:
            result.reject_reason = f"cooldown ({(self.min_signal_gap_s * 1000 - gap_ms) // 1000}s left)"
            return result

        # Look for cascade in the recent window
        win_ms = int(CFG.liq_window_seconds * 1000)
        liqs = self.market.liquidations_in_window(coin, win_ms, now_ms)
        if not liqs:
            result.reject_reason = "no liq in window"
            return result

        long_usd = sum(t.size_usd for t in liqs if not t.aggressor_buy)   # aggressor 'A' = longs liquidated
        short_usd = sum(t.size_usd for t in liqs if t.aggressor_buy)      # aggressor 'B' = shorts liquidated

        threshold = self._threshold_for(coin)
        if max(long_usd, short_usd) < threshold:
            result.reject_reason = f"cascade below threshold (long=${long_usd:,.0f} short=${short_usd:,.0f} need ${threshold:,.0f})"
            return result

        # Determine dominant liquidated side
        if long_usd >= short_usd:
            liquidated_side = "long"
            our_dir = "long"           # fade: price down, we buy
            cascade_total = long_usd
        else:
            liquidated_side = "short"
            our_dir = "short"          # fade: price up, we sell
            cascade_total = short_usd

        # Mark cascade end (for state tracking)
        cascade_end_ms = max(t.ts_ms for t in liqs)

        # Wait window since cascade ended
        age_since_cascade_s = (now_ms - cascade_end_ms) / 1000.0
        if age_since_cascade_s < CFG.liq_post_cascade_wait_s:
            result.reject_reason = f"waiting post-cascade ({age_since_cascade_s:.1f}s of {CFG.liq_post_cascade_wait_s:.1f}s)"
            return result
        if age_since_cascade_s > CFG.liq_max_post_cascade_age_s:
            result.reject_reason = f"cascade stale ({age_since_cascade_s:.1f}s old)"
            return result

        # Aggressor flow confirmation: in the period FROM cascade end to now,
        # aggressor flow should have flipped in our favor.
        flow_window_ms = max(int((now_ms - cascade_end_ms)), 1500)
        ratio = self.market.aggressor_buy_ratio(coin, flow_window_ms, now_ms)
        if ratio is None:
            result.reject_reason = "no flow data post-cascade"
            return result
        if our_dir == "long" and ratio < 0.55:
            result.reject_reason = f"flow not flipped to buyers (ratio={ratio:.2f})"
            return result
        if our_dir == "short" and ratio > 0.45:
            result.reject_reason = f"flow not flipped to sellers (ratio={ratio:.2f})"
            return result

        # ATR for sizing
        candles = self.market.get_closed_candles(coin)
        atr = atr_from_candles(candles, period=14)
        if atr is None or atr <= 0:
            result.reject_reason = f"insufficient candle history ({len(candles)} bars)"
            return result

        last_px = self.market.latest_price(coin)
        if last_px is None or last_px <= 0:
            result.reject_reason = "no price"
            return result

        # Build signal
        if our_dir == "long":
            stop = last_px - atr * CFG.liq_stop_atr_mult
            target = last_px + atr * CFG.liq_target_atr_mult
        else:
            stop = last_px + atr * CFG.liq_stop_atr_mult
            target = last_px - atr * CFG.liq_target_atr_mult

        # Sanity: target/stop must be on correct sides of entry and reasonable
        if our_dir == "long" and not (stop < last_px < target):
            result.reject_reason = "invalid stop/target geometry"
            return result
        if our_dir == "short" and not (target < last_px < stop):
            result.reject_reason = "invalid stop/target geometry"
            return result

        # Risk-based sizing happens in the executor; we just attach metadata here.
        result.direction = our_dir
        result.entry_px = last_px
        result.stop_px = float(stop)
        result.target_px = float(target)
        result.meta = {
            "cascade_total_usd": float(cascade_total),
            "liquidated_side": liquidated_side,
            "n_liq_events": len(liqs),
            "atr_1m": float(atr),
            "flow_ratio": float(ratio),
            "post_cascade_age_s": float(age_since_cascade_s),
        }

        self._last_signal_ms[coin] = now_ms
        self._last_cascade_end_ms[coin] = cascade_end_ms
        return result
