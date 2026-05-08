"""
Paper executor — simulates fills with realistic slippage.

Slippage model:
  bps = slippage_base + slippage_per_vol_pct * realized_vol_pct
  where realized_vol_pct = ATR_1m / mid * 100
Plus fixed taker_fee_bps.

So a typical fill at 0.5% realized vol:
  slip = 5 + 15*0.5 = 12.5 bps
  fee  = 4.5 bps
  total round-trip cost ≈ (12.5+4.5)*2 = 34 bps

This is intentionally conservative; live results should beat paper if
post_only/maker fills are used in live mode.
"""
from __future__ import annotations
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from ..config import CFG
from ..db import session_scope, PaperTrade, Signal, log_activity, get_state, update_state
from ..strategies.types import StrategyResult
from ..ws.market_state import MarketState
from .types import OpenPosition

log = logging.getLogger(__name__)


class PaperExecutor:
    def __init__(self, market: MarketState):
        self.market = market

    def _realized_vol_pct(self, coin: str) -> float:
        """ATR/mid * 100 in pct. Falls back to a conservative 0.5% if no data."""
        from ..strategies.math_utils import atr_from_candles
        candles = self.market.get_closed_candles(coin)
        atr = atr_from_candles(candles, period=14)
        last = self.market.latest_price(coin)
        if atr is None or last is None or last <= 0:
            return 0.5
        return (atr / last) * 100.0

    def _apply_slippage(self, coin: str, direction: str, intended_px: float) -> float:
        vol_pct = self._realized_vol_pct(coin)
        slip_bps = CFG.slippage_bps_base + CFG.slippage_bps_per_vol_pct * vol_pct
        # Slippage is always against us
        if direction == "long":
            return intended_px * (1 + slip_bps / 10_000.0)
        else:
            return intended_px * (1 - slip_bps / 10_000.0)

    async def open(self, sig: StrategyResult, signal_id: int, size_usd: float) -> Optional[OpenPosition]:
        if not sig.has_signal or size_usd <= 0:
            return None

        fill_px = self._apply_slippage(sig.coin, sig.direction, sig.entry_px)
        # Entry fee on the notional
        entry_fee_usd = size_usd * (CFG.taker_fee_bps / 10_000.0)
        now_ms = int(time.time() * 1000)

        async with session_scope() as s:
            tr = PaperTrade(
                signal_id=signal_id,
                coin=sig.coin,
                strategy=sig.strategy,
                direction=sig.direction,
                size_usd=size_usd,
                leverage=1.0,
                entry_px=sig.entry_px,
                entry_fill_px=fill_px,
                stop_px=sig.stop_px,
                target_px=sig.target_px,
                fees_usd=entry_fee_usd,
                opened_ms=now_ms,
                status="open",
            )
            s.add(tr)
            await s.flush()
            trade_id = tr.id

        await log_activity(
            "info", "exec",
            f"PAPER OPEN {sig.strategy} {sig.coin} {sig.direction} ${size_usd:.0f} @ ${fill_px:.4f} "
            f"(SL ${sig.stop_px:.4f} / TP ${sig.target_px:.4f})",
            meta={"trade_id": trade_id, "intended": sig.entry_px, "fill": fill_px},
        )

        return OpenPosition(
            id=trade_id, kind="paper", coin=sig.coin, strategy=sig.strategy,
            direction=sig.direction, size_usd=size_usd, entry_fill_px=fill_px,
            stop_px=sig.stop_px, target_px=sig.target_px,
            opened_ms=now_ms, fees_usd=entry_fee_usd,
        )

    async def close(self, trade_id: int, exit_px_intended: float, reason: str) -> Optional[float]:
        """Close the paper trade. Returns realized pnl_usd, or None on error."""
        async with session_scope() as s:
            tr = await s.get(PaperTrade, trade_id)
            if tr is None or tr.status != "open":
                return None
            # Apply slippage in the opposite direction (closing)
            close_dir = "short" if tr.direction == "long" else "long"
            fill_px = self._apply_slippage(tr.coin, close_dir, exit_px_intended)
            # Exit fee
            exit_fee = tr.size_usd * (CFG.taker_fee_bps / 10_000.0)
            total_fees = tr.fees_usd + exit_fee

            # PnL on a $size_usd position from entry to fill_px
            ret = (fill_px - tr.entry_fill_px) / tr.entry_fill_px
            if tr.direction == "short":
                ret = -ret
            pnl_gross = tr.size_usd * ret
            pnl_net = pnl_gross - total_fees

            tr.exit_px = float(fill_px)
            tr.exit_reason = reason
            tr.pnl_usd = float(pnl_net)
            tr.fees_usd = float(total_fees)
            tr.closed_ms = int(time.time() * 1000)
            tr.status = "closed"

        # Update equity & consecutive-loss counter
        st = await get_state()
        new_equity = st.paper_equity_usd + pnl_net
        new_peak = max(st.paper_peak_equity_usd, new_equity)
        # Daily P&L tracking
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if st.daily_pnl_date == today:
            new_daily = st.daily_pnl_usd + pnl_net
        else:
            new_daily = pnl_net
        # Consecutive losses: increment on loss, reset on win/breakeven.
        # We use <0 so a $0.00 fee-only outcome doesn't pile up.
        if pnl_net < 0:
            new_consec = st.consecutive_losses + 1
        else:
            new_consec = 0
        await update_state(
            paper_equity_usd=new_equity,
            paper_peak_equity_usd=new_peak,
            paper_realized_pnl_usd=st.paper_realized_pnl_usd + pnl_net,
            daily_pnl_usd=new_daily,
            daily_pnl_date=today,
            consecutive_losses=new_consec,
        )

        await log_activity(
            "info" if pnl_net >= 0 else "warn", "exec",
            f"PAPER CLOSE {tr.strategy} {tr.coin} {tr.direction} reason={reason} "
            f"PnL=${pnl_net:+.2f} (gross=${pnl_gross:+.2f}, fees=${total_fees:.2f})",
            meta={"trade_id": trade_id, "exit_px": fill_px},
        )
        return pnl_net
