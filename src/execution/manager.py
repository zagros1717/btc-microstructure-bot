"""
Trade manager — monitors open positions and closes them on:
  - stop loss hit
  - target hit
  - time stop (per-strategy)
  - manual /close call from API

Runs as a background asyncio task, polls every ~250ms.
"""
from __future__ import annotations
import asyncio
import logging
import time
from typing import Optional

from sqlalchemy import select

from ..config import CFG
from ..db import session_scope, PaperTrade, LiveTrade, log_activity
from ..ws.market_state import MarketState
from .paper_executor import PaperExecutor
from .live_executor import LiveExecutor

log = logging.getLogger(__name__)


class TradeManager:
    POLL_INTERVAL_S = 0.25

    def __init__(self, market: MarketState, paper: PaperExecutor, live: LiveExecutor, risk_manager=None):
        self.market = market
        self.paper = paper
        self.live = live
        self.risk_manager = risk_manager
        self._stop = asyncio.Event()

    async def stop(self) -> None:
        self._stop.set()

    def _time_stop_seconds(self, strategy: str) -> int:
        if strategy == "liquidation_fade":
            return CFG.liq_time_stop_seconds
        if strategy == "liquidity_wall":
            return CFG.wall_time_stop_seconds
        return 600

    async def run(self) -> None:
        log.info("TradeManager started")
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as e:
                log.exception("manager tick error: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.POLL_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
        log.info("TradeManager stopped")

    async def _tick(self) -> None:
        now_ms = int(time.time() * 1000)

        # Paper trades
        async with session_scope() as s:
            res = await s.execute(select(PaperTrade).where(PaperTrade.status == "open"))
            paper_trades = list(res.scalars().all())

        for tr in paper_trades:
            last = self.market.latest_price(tr.coin)
            if last is None or last <= 0:
                continue

            # MAE/MFE update (USD pnl at current price, ignoring fees for excursion tracking)
            cur_pnl = self._unrealized_pnl(tr.direction, tr.entry_fill_px, last, tr.size_usd)
            new_mae = min(tr.mae_usd, cur_pnl)
            new_mfe = max(tr.mfe_usd, cur_pnl)
            if new_mae < tr.mae_usd or new_mfe > tr.mfe_usd:
                async with session_scope() as s2:
                    db_tr = await s2.get(PaperTrade, tr.id)
                    if db_tr is not None and db_tr.status == "open":
                        db_tr.mae_usd = float(new_mae)
                        db_tr.mfe_usd = float(new_mfe)

            reason = self._check_exit(
                direction=tr.direction, last_px=last,
                stop_px=tr.stop_px, target_px=tr.target_px,
                opened_ms=tr.opened_ms, now_ms=now_ms,
                time_stop_s=self._time_stop_seconds(tr.strategy),
            )
            if reason:
                pnl = await self.paper.close(tr.id, last, reason)
                if pnl is not None and pnl < 0 and self.risk_manager is not None:
                    self.risk_manager.record_loss(tr.coin)

        # Live trades — same MAE/MFE bookkeeping
        if self.live.enabled:
            async with session_scope() as s:
                res = await s.execute(select(LiveTrade).where(LiveTrade.status == "open"))
                live_trades = list(res.scalars().all())
            for tr in live_trades:
                last = self.market.latest_price(tr.coin)
                if last is None or last <= 0 or not tr.entry_fill_px:
                    continue
                cur_pnl = self._unrealized_pnl(tr.direction, tr.entry_fill_px, last, tr.size_usd)
                new_mae = min(tr.mae_usd, cur_pnl)
                new_mfe = max(tr.mfe_usd, cur_pnl)
                if new_mae < tr.mae_usd or new_mfe > tr.mfe_usd:
                    async with session_scope() as s2:
                        db_tr = await s2.get(LiveTrade, tr.id)
                        if db_tr is not None and db_tr.status == "open":
                            db_tr.mae_usd = float(new_mae)
                            db_tr.mfe_usd = float(new_mfe)

                reason = self._check_exit(
                    direction=tr.direction, last_px=last,
                    stop_px=tr.stop_px, target_px=tr.target_px,
                    opened_ms=tr.opened_ms, now_ms=now_ms,
                    time_stop_s=self._time_stop_seconds(tr.strategy),
                )
                if reason:
                    await self.live.close(tr.id, last, reason)

    @staticmethod
    def _unrealized_pnl(direction: str, entry: float, last: float, size_usd: float) -> float:
        if entry <= 0:
            return 0.0
        ret = (last - entry) / entry
        if direction == "short":
            ret = -ret
        return size_usd * ret

    @staticmethod
    def _check_exit(direction: str, last_px: float, stop_px: float, target_px: float,
                    opened_ms: int, now_ms: int, time_stop_s: int) -> Optional[str]:
        # Stop and target
        if direction == "long":
            if last_px <= stop_px:
                return "stop"
            if last_px >= target_px:
                return "target"
        else:
            if last_px >= stop_px:
                return "stop"
            if last_px <= target_px:
                return "target"
        # Time stop
        if (now_ms - opened_ms) / 1000.0 >= time_stop_s:
            return "time_stop"
        return None

    async def emergency_close_all(self, reason: str = "emergency_stop") -> dict:
        """Close every open trade immediately at last price."""
        closed = {"paper": 0, "live": 0}
        async with session_scope() as s:
            res = await s.execute(select(PaperTrade).where(PaperTrade.status == "open"))
            ptrs = list(res.scalars().all())
        for tr in ptrs:
            last = self.market.latest_price(tr.coin) or tr.entry_fill_px
            if await self.paper.close(tr.id, last, reason) is not None:
                closed["paper"] += 1

        if self.live.enabled:
            async with session_scope() as s:
                res = await s.execute(select(LiveTrade).where(LiveTrade.status == "open"))
                ltrs = list(res.scalars().all())
            for tr in ltrs:
                last = self.market.latest_price(tr.coin) or tr.entry_fill_px
                if await self.live.close(tr.id, last, reason) is not None:
                    closed["live"] += 1

        await log_activity("warn", "exec", f"emergency_close_all: {closed}")
        return closed
