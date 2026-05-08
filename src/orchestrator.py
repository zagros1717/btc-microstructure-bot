"""
Orchestrator — the brain that ties it all together.

Loop (every ORCH_TICK_S=0.5s):
  For each enabled strategy (from BotState, NOT from CFG):
    For each relevant coin:
      Evaluate strategy
      If signal:  risk-check → maybe open paper (and live, if armed) → persist
      If reject:  bucket-count to signal_reject_stats (sampled persist)

Background loops:
  - WS listener (run forever, reconnect)
  - TradeManager (poll open trades for SL/TP/time stop)
  - Heartbeat (mark BotState alive)
  - Wall persistence (snapshot wall lifecycle into DB on state changes)
  - Reconciliation (compare live trades vs HL exchange state; pause on mismatch)
  - Feed validation (after first hour, if no liquidations seen → auto-disable liq_fade)
"""
from __future__ import annotations
import asyncio
import logging
import time
from typing import Iterable

from sqlalchemy import select

from .config import CFG
from .db import (
    session_scope, Signal, WallEvent, log_activity, update_state, get_state,
    record_reject_stat,
)
from .strategies import LiquidationFadeStrategy, LiquidityWallStrategy
from .ws.market_state import MarketState
from .ws.hl_listener import HyperliquidListener
from .execution import PaperExecutor, LiveExecutor, TradeManager
from .risk import RiskManager, compute_size_usd

log = logging.getLogger(__name__)

ORCH_TICK_S = 0.5
HEARTBEAT_INTERVAL_S = 5
WALL_PERSIST_INTERVAL_S = 5
FEED_VALIDATION_AFTER_S = 3600  # 1 hour


class Orchestrator:
    def __init__(self):
        self.market = MarketState(CFG.coins)
        self.liq_strategy = LiquidationFadeStrategy(self.market)
        self.wall_strategy = LiquidityWallStrategy(self.market)
        self.paper = PaperExecutor(self.market)
        self.live = LiveExecutor(self.market)
        self.risk = RiskManager(self.market)
        self.manager = TradeManager(self.market, self.paper, self.live, self.risk)
        self.listener = HyperliquidListener(
            self.market,
            on_liquidation=self._on_liquidation,
            on_l2_update=self._on_l2,
        )
        self._stop = asyncio.Event()
        self._started_ms = int(time.time() * 1000)
        # Cache of wall_id -> last persisted state, to know when to upsert
        self._wall_persist_state: dict[str, str] = {}
        # Whether feed validation has already auto-disabled liq_fade
        self._feed_validated = False

    async def stop(self) -> None:
        self._stop.set()
        await self.listener.stop()
        await self.manager.stop()

    async def _on_liquidation(self, tick, coin: str) -> None:
        if tick.size_usd >= 100_000:
            await log_activity(
                "info", "strategy",
                f"liquidation {coin} side={'short' if tick.aggressor_buy else 'long'} "
                f"${tick.size_usd:,.0f} @ ${tick.px:.4f}",
            )

    async def _on_l2(self, coin: str, snap) -> None:
        # Wall tracking on every L2 update (BTC only)
        if coin == "BTC":
            try:
                self.wall_strategy.on_l2(snap)
            except Exception as e:
                log.exception("wall_strategy.on_l2 failed: %s", e)

    async def run(self) -> None:
        log.info(
            "Orchestrator starting; coins=%s liq=%s wall=%s live_capable=%s",
            CFG.coins, CFG.enable_liquidation_fade, CFG.enable_liquidity_wall,
            self.live.capable,
        )
        await log_activity(
            "info", "system",
            f"bot starting — coins={','.join(CFG.coins)} "
            f"liq_fade={'on' if CFG.enable_liquidation_fade else 'off'} "
            f"wall={'on' if CFG.enable_liquidity_wall else 'off'} "
            f"live_capable={self.live.capable} (orders refused unless armed)",
        )

        ws_task = asyncio.create_task(self.listener.run(), name="ws")
        mgr_task = asyncio.create_task(self.manager.run(), name="mgr")
        hb_task = asyncio.create_task(self._heartbeat_loop(), name="hb")
        wall_task = asyncio.create_task(self._wall_persist_loop(), name="wall")
        recon_task = asyncio.create_task(self._reconcile_loop(), name="recon")
        feed_task = asyncio.create_task(self._feed_validation_loop(), name="feed")

        try:
            while not self._stop.is_set():
                try:
                    await self._tick()
                except Exception as e:
                    log.exception("orch tick error: %s", e)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=ORCH_TICK_S)
                except asyncio.TimeoutError:
                    pass
        finally:
            for t in (ws_task, mgr_task, hb_task, wall_task, recon_task, feed_task):
                t.cancel()
            for t in (ws_task, mgr_task, hb_task, wall_task, recon_task, feed_task):
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._stop.is_set():
                await update_state(last_heartbeat_ms=int(time.time() * 1000))
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=HEARTBEAT_INTERVAL_S)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _wall_persist_loop(self) -> None:
        """Periodically write wall lifecycle changes to wall_events table."""
        try:
            while not self._stop.is_set():
                try:
                    await self._persist_walls_once()
                except Exception as e:
                    log.exception("wall persist failed: %s", e)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=WALL_PERSIST_INTERVAL_S)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _persist_walls_once(self) -> None:
        """For each wall in memory, upsert its state to DB if changed."""
        from sqlalchemy import and_
        for w in list(self.wall_strategy.walls.values()):
            prev = self._wall_persist_state.get(w.wall_id)
            if prev == w.state:
                continue  # no change
            # Upsert: find by (wall_id, detected_ms) which is unique-ish
            async with session_scope() as s:
                res = await s.execute(
                    select(WallEvent).where(and_(
                        WallEvent.wall_id == w.wall_id,
                        WallEvent.detected_ms == w.detected_ms,
                    )).limit(1)
                )
                row = res.scalars().first()
                if row is None:
                    row = WallEvent(
                        coin="BTC", wall_id=w.wall_id, side=w.side, px=w.px,
                        size_usd_initial=w.size_usd_initial,
                        size_usd_min=w.size_usd_min,
                        size_usd_final=w.size_usd_current,
                        detected_ms=w.detected_ms,
                        confirmed_ms=w.confirmed_ms,
                        final_state=w.state,
                    )
                    s.add(row)
                else:
                    row.size_usd_min = w.size_usd_min
                    row.size_usd_final = w.size_usd_current
                    row.confirmed_ms = w.confirmed_ms
                    row.final_state = w.state
                    if w.state in ("vanished", "expired", "fired", "consumed"):
                        row.final_ms = int(time.time() * 1000)
            self._wall_persist_state[w.wall_id] = w.state

    async def _reconcile_loop(self) -> None:
        """Periodically reconcile DB live trades with HL state."""
        try:
            while not self._stop.is_set():
                if self.live.capable:
                    try:
                        await self.live.reconcile_with_exchange()
                    except Exception as e:
                        log.warning("reconciliation error: %s", e)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=CFG.reconcile_interval_s)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _feed_validation_loop(self) -> None:
        """After 1 hour, if no liquidation-flagged trades have arrived,
        explicitly mark liq_feed_status='unavailable' AND disable liq_fade.
        Operator can re-enable manually via /api/liq_feed/override after
        inspecting raw_ws_samples.
        """
        try:
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=FEED_VALIDATION_AFTER_S,
                )
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set() or self._feed_validated:
                return

            counters = self.listener.counters
            trades_total = counters.get("trades_total", 0)
            with_liq = counters.get("trades_with_liq_field", 0)
            now_ms = int(time.time() * 1000)

            await update_state(liq_feed_checked_ms=now_ms)

            if trades_total < 100:
                # Feed barely working — don't make a decision yet
                await log_activity(
                    "warn", "system",
                    f"feed validation inconclusive: only {trades_total} trades "
                    f"after {FEED_VALIDATION_AFTER_S}s — feed may be impaired. "
                    f"liq_feed_status remains 'unknown', liq_fade allowed to run.",
                )
                return

            if with_liq == 0:
                # Definitive: feed is producing trades but no liquidations.
                # Mark status explicitly so operator/dashboard see it as
                # 'unavailable' (different from 'manually disabled').
                await update_state(
                    liq_feed_status="unavailable",
                    liq_fade_enabled=False,
                )
                await log_activity(
                    "error", "system",
                    f"FEED VALIDATION: 0 liquidation-flagged trades in {trades_total} "
                    f"trades over {FEED_VALIDATION_AFTER_S}s. liq_feed_status="
                    f"'unavailable'. liquidation_fade is now refusing to run. "
                    f"Inspect raw_ws_samples; if data shape needs a different "
                    f"parser, fix and call POST /api/liq_feed/override to clear.",
                )
            else:
                # Note: by this point listener should have already set status
                # to 'validated' on first liq seen. This is a belt-and-braces
                # safeguard for when the listener missed (e.g. DB error there).
                await update_state(liq_feed_status="validated")
                await log_activity(
                    "info", "system",
                    f"feed validation OK: {with_liq} liq-flagged of {trades_total} "
                    f"trades after {FEED_VALIDATION_AFTER_S}s",
                )
            self._feed_validated = True
        except asyncio.CancelledError:
            pass

    async def _tick(self) -> None:
        now_ms = int(time.time() * 1000)

        # Read DB-backed toggles once per tick (cached internally by RiskManager)
        st = await self.risk.get_state(force=False)

        # Liquidation Fade: requires both runtime toggle AND feed status != unavailable.
        # Status "unknown" is allowed (we're still in the validation window).
        # Status "validated" is the happy path.
        # Status "unavailable" means the feed has been confirmed dead — strategy
        # cannot run regardless of the operator toggle.
        liq_feed_status = st.get("liq_feed_status", "unknown")
        liq_can_run = (
            st.get("liq_fade_enabled", True)
            and liq_feed_status != "unavailable"
        )

        if liq_can_run:
            for coin in CFG.coins:
                await self._tick_strategy("liquidation_fade", coin, now_ms)

        if st.get("wall_enabled", True):
            await self._tick_strategy("liquidity_wall", "BTC", now_ms)

    async def _tick_strategy(self, strat: str, coin: str, now_ms: int) -> None:
        try:
            if strat == "liquidation_fade":
                res = self.liq_strategy.evaluate(coin, now_ms)
            elif strat == "liquidity_wall":
                res = self.wall_strategy.evaluate(now_ms)
            else:
                return
        except Exception as e:
            log.exception("strategy %s error on %s: %s", strat, coin, e)
            return

        if not res.has_signal:
            # Bucket-count rejections so we can see why nothing fires
            if res.reject_reason:
                try:
                    await record_reject_stat(strat, coin, res.reject_reason)
                except Exception:
                    pass
            return

        # Risk check
        ok, reject = await self.risk.check_can_trade(coin, strat)
        if not ok:
            async with session_scope() as s:
                s.add(Signal(
                    ts_ms=now_ms, coin=coin, strategy=strat, direction=res.direction,
                    accepted=False, reject_reason=f"risk: {reject}",
                    entry_px=res.entry_px or 0, stop_px=res.stop_px or 0,
                    target_px=res.target_px or 0, size_usd=0, meta=res.meta,
                ))
            await log_activity("info", "risk", f"{strat} {coin} {res.direction} rejected: {reject}")
            return

        # Sizing — reuse cached state
        st = await self.risk.get_state(force=False)
        size_usd = compute_size_usd(st["paper_equity_usd"], res.entry_px, res.stop_px)
        if size_usd <= 0:
            return

        # Persist signal as accepted
        async with session_scope() as s:
            sig = Signal(
                ts_ms=now_ms, coin=coin, strategy=strat, direction=res.direction,
                accepted=True, entry_px=res.entry_px, stop_px=res.stop_px,
                target_px=res.target_px, size_usd=size_usd, meta=res.meta,
            )
            s.add(sig)
            await s.flush()
            signal_id = sig.id

        # Open paper always
        await self.paper.open(res, signal_id, size_usd)
        # Open live only if SDK capable AND runtime-armed (live executor enforces both)
        if self.live.capable:
            await self.live.open(res, signal_id, size_usd)
