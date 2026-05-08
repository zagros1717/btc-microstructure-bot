"""
Risk manager — enforces all global trading limits.

Checks (in order; first failure short-circuits):
  1. Bot paused?
  2. WebSocket fresh (global)?
  3. Per-coin trade freshness (this coin alive?)
  4. Daily loss limit hit?
  5. Drawdown circuit breaker?
  6. Consecutive-loss circuit breaker?
  7. Per-coin loss cooldown?
  8. Max concurrent positions (paper + live)?
  9. Max per-strategy positions?
 10. Max per-coin positions?
"""
from __future__ import annotations
import logging
import time
from datetime import datetime, timezone
from typing import Optional, Tuple, TYPE_CHECKING

from sqlalchemy import select, func, and_, or_

from ..config import CFG
from ..db import session_scope, BotState, PaperTrade, LiveTrade, log_activity, update_state

if TYPE_CHECKING:
    from ..ws.market_state import MarketState

log = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, market: Optional["MarketState"] = None):
        self.market = market  # used for per-coin freshness check
        self._last_state_check_ms = 0
        self._cached_state: Optional[dict] = None
        self._loss_cooldown_ms: dict[str, int] = {}

    def attach_market(self, market: "MarketState") -> None:
        """Set market state ref after construction (orchestrator wires this)."""
        self.market = market

    async def _refresh_state(self) -> dict:
        async with session_scope() as s:
            st = await s.get(BotState, 1)
            if st is None:
                return self._defaults()
            return {
                "is_paused": st.is_paused,
                "pause_reason": st.pause_reason,
                "paper_equity_usd": st.paper_equity_usd,
                "paper_peak_equity_usd": st.paper_peak_equity_usd,
                "daily_pnl_usd": st.daily_pnl_usd,
                "daily_pnl_date": st.daily_pnl_date,
                "consecutive_losses": st.consecutive_losses,
                "ws_connected": st.ws_connected,
                "last_ws_msg_ms": st.last_ws_msg_ms,
                "liq_fade_enabled": st.liq_fade_enabled,
                "wall_enabled": st.wall_enabled,
                "live_armed": st.live_armed,
                "liq_feed_status": st.liq_feed_status,
                "liq_feed_validated_ms": st.liq_feed_validated_ms,
                "liq_feed_checked_ms": st.liq_feed_checked_ms,
            }

    def _defaults(self) -> dict:
        return {
            "is_paused": False, "pause_reason": None,
            "paper_equity_usd": CFG.paper_starting_balance_usd,
            "paper_peak_equity_usd": CFG.paper_starting_balance_usd,
            "daily_pnl_usd": 0.0, "daily_pnl_date": None,
            "consecutive_losses": 0,
            "ws_connected": False, "last_ws_msg_ms": 0,
            "liq_fade_enabled": True, "wall_enabled": True, "live_armed": False,
            "liq_feed_status": "unknown",
            "liq_feed_validated_ms": 0,
            "liq_feed_checked_ms": 0,
        }

    async def get_state(self, force: bool = False) -> dict:
        now_ms = int(time.time() * 1000)
        if force or self._cached_state is None or now_ms - self._last_state_check_ms > 1000:
            self._cached_state = await self._refresh_state()
            self._last_state_check_ms = now_ms
        return self._cached_state

    def record_loss(self, coin: str) -> None:
        self._loss_cooldown_ms[coin] = int(time.time() * 1000)

    async def check_can_trade(
        self, coin: str, strategy: str,
    ) -> Tuple[bool, Optional[str]]:
        """Return (ok, reject_reason)."""
        st = await self.get_state(force=True)
        now_ms = int(time.time() * 1000)

        if st["is_paused"]:
            return False, f"bot paused: {st.get('pause_reason') or 'unknown'}"

        # Strategy-level toggle (DB-backed)
        if strategy == "liquidation_fade":
            if not st.get("liq_fade_enabled", True):
                return False, "liq_fade disabled at runtime"
            # Belt-and-braces: orchestrator already gates this, but a direct
            # caller could bypass that. If feed status is "unavailable",
            # absolutely refuse — there is no valid liquidation data.
            if st.get("liq_feed_status") == "unavailable":
                return False, "liq feed unavailable (no valid liquidation source)"
        if strategy == "liquidity_wall" and not st.get("wall_enabled", True):
            return False, "wall disabled at runtime"

        # Global WS freshness
        last = st.get("last_ws_msg_ms", 0) or 0
        if last > 0:
            age_s = (now_ms - last) / 1000.0
            if age_s > CFG.heartbeat_timeout_s:
                return False, f"WS stale ({age_s:.0f}s since last message)"

        # Per-coin trade freshness (the WS may be alive for *some* coin but
        # this specific one might not have traded recently)
        if self.market is not None:
            last_coin_ms = self.market.last_trade_ms.get(coin, 0)
            if last_coin_ms > 0:
                coin_age_s = (now_ms - last_coin_ms) / 1000.0
                if coin_age_s > CFG.coin_stale_seconds:
                    return False, f"{coin} stale ({coin_age_s:.0f}s)"

        # Daily loss limit
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if st.get("daily_pnl_date") == today:
            limit = CFG.paper_starting_balance_usd * (CFG.daily_loss_limit_pct / 100.0)
            if st["daily_pnl_usd"] <= -limit:
                await update_state(is_paused=True, pause_reason=f"daily loss limit hit (-${limit:.2f})")
                await log_activity("error", "risk", f"AUTO-PAUSE: daily loss limit hit (-${limit:.2f})")
                return False, "daily loss limit hit"

        # Drawdown circuit
        peak = max(st["paper_peak_equity_usd"], CFG.paper_starting_balance_usd)
        equity = st["paper_equity_usd"]
        dd_pct = (1 - equity / peak) * 100 if peak > 0 else 0
        if dd_pct >= CFG.drawdown_circuit_pct:
            await update_state(is_paused=True, pause_reason=f"drawdown circuit ({dd_pct:.1f}%)")
            await log_activity("error", "risk", f"AUTO-PAUSE: drawdown {dd_pct:.1f}% >= {CFG.drawdown_circuit_pct}%")
            return False, f"drawdown {dd_pct:.1f}%"

        # Consecutive-loss circuit
        if st["consecutive_losses"] >= CFG.max_consecutive_losses:
            await update_state(
                is_paused=True,
                pause_reason=f"{st['consecutive_losses']} consecutive losses",
            )
            await log_activity(
                "error", "risk",
                f"AUTO-PAUSE: {st['consecutive_losses']} consecutive losses "
                f">= {CFG.max_consecutive_losses}",
            )
            return False, "consecutive loss limit"

        # Per-coin loss cooldown
        last_loss_ms = self._loss_cooldown_ms.get(coin, 0)
        if last_loss_ms > 0:
            since_min = (now_ms - last_loss_ms) / 60_000.0
            if since_min < CFG.per_coin_cooldown_min:
                left = CFG.per_coin_cooldown_min - since_min
                return False, f"coin cooldown ({left:.0f} min left)"

        # Position limits — count BOTH paper and live as exposure
        async with session_scope() as s:
            # Total open
            total_q = select(func.count(PaperTrade.id)).where(PaperTrade.status == "open")
            total_paper = (await s.execute(total_q)).scalar() or 0
            total_live = (await s.execute(
                select(func.count(LiveTrade.id)).where(LiveTrade.status == "open")
            )).scalar() or 0
            total = total_paper + total_live

            if total >= CFG.max_concurrent_positions:
                return False, f"max concurrent positions ({total})"

            # Per-strategy
            strat_paper = (await s.execute(
                select(func.count(PaperTrade.id)).where(and_(
                    PaperTrade.status == "open",
                    PaperTrade.strategy == strategy,
                ))
            )).scalar() or 0
            strat_live = (await s.execute(
                select(func.count(LiveTrade.id)).where(and_(
                    LiveTrade.status == "open",
                    LiveTrade.strategy == strategy,
                ))
            )).scalar() or 0
            if strat_paper + strat_live >= CFG.max_positions_per_strategy:
                return False, f"max {strategy} positions ({strat_paper + strat_live})"

            # Per-coin
            coin_paper = (await s.execute(
                select(func.count(PaperTrade.id)).where(and_(
                    PaperTrade.status == "open",
                    PaperTrade.coin == coin,
                ))
            )).scalar() or 0
            coin_live = (await s.execute(
                select(func.count(LiveTrade.id)).where(and_(
                    LiveTrade.status == "open",
                    LiveTrade.coin == coin,
                ))
            )).scalar() or 0
            if coin_paper + coin_live >= CFG.max_positions_per_coin:
                return False, f"max {coin} positions ({coin_paper + coin_live})"

        return True, None
