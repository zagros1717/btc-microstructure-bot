"""
Live executor — real orders to Hyperliquid.

THREE-STAGE GATE (all must be true for an order to actually be sent):
  Stage 1 (compile-time-ish):  CFG.live_code_enabled == True
                                → SDK is even imported and initialized
  Stage 2 (load-time):          CFG.enable_live_execution == True
                                AND keys present
                                → Exchange client is constructed
  Stage 3 (runtime):            BotState.live_armed == True
                                → orders are actually sent

Plus per-order guards:
  - size_usd <= CFG.live_max_order_usd
  - slippage at entry within CFG.live_max_slippage_bps
  - if CFG.require_protected_exits, a reduce-only stop AND tp must be placeable

PROTECTED EXITS:
  Currently the SDK call to place reduce-only stop/TP is a placeholder
  (raises NotImplementedError). Until that is implemented, live orders are
  REFUSED when require_protected_exits=True. This is the safe default.

RECONCILIATION:
  reconcile_with_exchange() compares DB live_trades (status='open') with
  actual HL positions. Mismatches pause the bot.
"""
from __future__ import annotations
import asyncio
import logging
import time
from typing import Optional

from ..config import CFG
from ..db import session_scope, LiveTrade, log_activity, get_state, update_state
from ..strategies.types import StrategyResult
from ..ws.market_state import MarketState
from .types import OpenPosition

log = logging.getLogger(__name__)


class ProtectedExitsNotImplementedError(RuntimeError):
    """Raised when a live order would be sent without exchange-side stop/TP."""


class LiveExecutor:
    def __init__(self, market: MarketState):
        self.market = market
        self._sdk = None  # SDK Exchange instance
        self._info = None  # SDK Info instance for reads
        self._capable = False  # stages 1+2 — SDK ready
        self._init_error: Optional[str] = None

        # STAGE 1: live_code_enabled gate. If False, SDK is not even imported.
        if not CFG.live_code_enabled:
            log.info("LIVE_CODE_ENABLED=false → live execution code is fully disabled")
            return

        # STAGE 2: enable_live_execution + keys gate.
        if not CFG.enable_live_execution:
            log.info("ENABLE_LIVE=false → SDK will not be initialized this run")
            return
        if not (CFG.hl_account_address and CFG.hl_secret_key):
            log.warning("ENABLE_LIVE=true but keys missing → SDK not initialized")
            self._init_error = "missing keys"
            return

        try:
            # Lazy import — only happens if all gates above pass
            from hyperliquid.exchange import Exchange
            from hyperliquid.info import Info
            from eth_account import Account
            wallet = Account.from_key(CFG.hl_secret_key)
            self._info = Info(CFG.hl_rest_url, skip_ws=True)
            self._sdk = Exchange(
                wallet, CFG.hl_rest_url,
                account_address=CFG.hl_account_address,
            )
            self._capable = True
            log.warning(
                "LIVE EXECUTION CAPABLE for account %s — but live_armed must "
                "be set in DB before any order is sent",
                CFG.hl_account_address,
            )
        except Exception as e:
            log.error("live executor init failed: %s", e)
            self._init_error = str(e)
            self._capable = False

    @property
    def capable(self) -> bool:
        """True if the SDK was successfully initialized (stages 1+2 passed)."""
        return self._capable

    @property
    def enabled(self) -> bool:
        """Backward-compat alias. Prefer `capable`."""
        return self._capable

    @property
    def init_error(self) -> Optional[str]:
        return self._init_error

    async def is_armed(self) -> bool:
        """STAGE 3: check the runtime DB flag. False unless explicitly armed."""
        st = await get_state()
        return bool(getattr(st, "live_armed", False))

    async def _refuse(self, reason: str, level: str = "error") -> None:
        await log_activity(level, "exec", f"LIVE REFUSED: {reason}")

    async def open(
        self, sig: StrategyResult, signal_id: int, size_usd: float,
    ) -> Optional[OpenPosition]:
        """Send a live market order. Returns None if any gate refuses."""
        # All three stages required
        if not self._capable:
            await self._refuse(
                f"SDK not capable (init_error={self._init_error or 'gates closed'})",
                level="warn",
            )
            return None
        if not await self.is_armed():
            await self._refuse("live_armed=false in DB; not sending order", level="warn")
            return None

        # Per-order guards
        if size_usd > CFG.live_max_order_usd:
            await self._refuse(
                f"order ${size_usd:.0f} > cap ${CFG.live_max_order_usd:.0f}",
            )
            return None

        last = self.market.latest_price(sig.coin)
        if last is None or last <= 0:
            await self._refuse(f"no price for {sig.coin}")
            return None

        slip_bps = abs(last - sig.entry_px) / sig.entry_px * 10_000
        if slip_bps > CFG.live_max_slippage_bps:
            await self._refuse(
                f"slippage {slip_bps:.1f}bps > cap {CFG.live_max_slippage_bps:.0f}bps",
            )
            return None

        # PROTECTED EXITS — hard guard
        if CFG.require_protected_exits:
            await self._refuse(
                "require_protected_exits=true and exchange-side stops are NOT YET "
                "implemented. Live orders will be refused until we add reduce-only "
                "trigger orders. To override (DANGEROUS, paper-only sim), set "
                "REQUIRE_PROTECTED_EXITS=false explicitly.",
            )
            # Auto-pause to make the operator aware
            await update_state(
                live_armed=False,
                pause_reason="live attempted but protected exits not implemented",
            )
            return None

        # If we get here, require_protected_exits was explicitly set False —
        # we still try to place stop/TP and degrade gracefully if that fails.
        size_coin = size_usd / last
        now_ms = int(time.time() * 1000)

        # Record intent first
        async with session_scope() as s:
            tr = LiveTrade(
                signal_id=signal_id, coin=sig.coin, strategy=sig.strategy,
                direction=sig.direction, size_usd=size_usd,
                entry_px=sig.entry_px, stop_px=sig.stop_px, target_px=sig.target_px,
                opened_ms=now_ms, status="pending",
            )
            s.add(tr)
            await s.flush()
            trade_id = tr.id

        # ENTRY
        try:
            is_buy = sig.direction == "long"
            loop = asyncio.get_running_loop()
            order_result = await loop.run_in_executor(
                None,
                lambda: self._sdk.market_open(  # type: ignore
                    name=sig.coin, is_buy=is_buy, sz=size_coin,
                    slippage=CFG.live_max_slippage_bps / 10_000.0,
                ),
            )
            if order_result.get("status") != "ok":
                raise RuntimeError(f"market_open: {order_result}")
            statuses = order_result.get("response", {}).get("data", {}).get("statuses", [{}])
            fill_px = float(statuses[0].get("filled", {}).get("avgPx", last))
            oid = str(statuses[0].get("filled", {}).get("oid", ""))
        except Exception as e:
            async with session_scope() as s:
                tr = await s.get(LiveTrade, trade_id)
                if tr is not None:
                    tr.status = "failed"
                    tr.error = str(e)[:1000]
                    tr.closed_ms = int(time.time() * 1000)
            await log_activity("error", "exec", f"live entry failed: {e}")
            return None

        # PROTECTED EXITS — best effort. If they fail, we close the position
        # immediately (better to take a tiny loss than carry unprotected risk).
        stop_oid = None
        target_oid = None
        try:
            stop_oid, target_oid = await self._place_protected_exits(
                coin=sig.coin, direction=sig.direction, size_coin=size_coin,
                stop_px=sig.stop_px, target_px=sig.target_px,
            )
        except ProtectedExitsNotImplementedError:
            await log_activity(
                "error", "exec",
                "protected exits not implemented — closing live position immediately",
            )
            try:
                await self._market_close(sig.coin, size_coin, sig.direction)
            except Exception as e:
                await log_activity("error", "exec", f"emergency close failed: {e}")
            await update_state(
                is_paused=True, live_armed=False,
                pause_reason="protected exits not implemented; live disarmed",
            )
            return None
        except Exception as e:
            await log_activity(
                "error", "exec",
                f"placing protected exits failed: {e}; closing position",
            )
            try:
                await self._market_close(sig.coin, size_coin, sig.direction)
            except Exception:
                pass
            return None

        async with session_scope() as s:
            tr = await s.get(LiveTrade, trade_id)
            tr.entry_fill_px = fill_px
            tr.hl_order_id = oid
            tr.hl_stop_oid = stop_oid
            tr.hl_target_oid = target_oid
            tr.status = "open"

        await log_activity(
            "info", "exec",
            f"LIVE OPEN {sig.strategy} {sig.coin} {sig.direction} ${size_usd:.0f} @ ${fill_px:.4f}",
            meta={"trade_id": trade_id, "oid": oid,
                  "stop_oid": stop_oid, "target_oid": target_oid},
        )

        return OpenPosition(
            id=trade_id, kind="live", coin=sig.coin, strategy=sig.strategy,
            direction=sig.direction, size_usd=size_usd, entry_fill_px=fill_px,
            stop_px=sig.stop_px, target_px=sig.target_px,
            opened_ms=now_ms, fees_usd=0.0,
        )

    async def _place_protected_exits(
        self, *, coin: str, direction: str, size_coin: float,
        stop_px: float, target_px: float,
    ) -> tuple[Optional[str], Optional[str]]:
        """Place reduce-only stop and TP trigger orders on the exchange.

        TODO: Implement using SDK's order() with trigger configuration. The
        Hyperliquid Python SDK supports trigger orders via the order() call
        with order_type={"trigger": {"isMarket": True, "triggerPx": "...",
        "tpsl": "sl"|"tp"}}. Until properly tested on testnet, this raises.
        """
        raise ProtectedExitsNotImplementedError(
            "exchange-side stop/TP placement not yet implemented. "
            "See live_executor._place_protected_exits TODO."
        )

    async def _market_close(self, coin: str, size_coin: float, direction: str) -> None:
        """Emergency market close. Used when protected-exits placement fails."""
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self._sdk.market_close(  # type: ignore
                coin=coin, sz=size_coin,
                slippage=CFG.live_max_slippage_bps / 10_000.0,
            ),
        )
        if result.get("status") != "ok":
            raise RuntimeError(str(result))

    async def close(self, trade_id: int, exit_px_intended: float, reason: str) -> Optional[float]:
        if not self._capable:
            return None
        async with session_scope() as s:
            tr = await s.get(LiveTrade, trade_id)
            if tr is None or tr.status != "open":
                return None
            entry_fill_px = tr.entry_fill_px
            size_usd = tr.size_usd
            coin = tr.coin
            direction = tr.direction
            strategy = tr.strategy
            stop_oid = tr.hl_stop_oid
            target_oid = tr.hl_target_oid

        # Cancel any standing protected-exit orders first
        for oid in (stop_oid, target_oid):
            if oid:
                try:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(
                        None, lambda o=oid: self._sdk.cancel(coin, int(o)),  # type: ignore
                    )
                except Exception as e:
                    log.warning("cancel oid=%s failed: %s", oid, e)

        try:
            size_coin = size_usd / entry_fill_px if entry_fill_px else 0
            loop = asyncio.get_running_loop()
            order_result = await loop.run_in_executor(
                None,
                lambda: self._sdk.market_close(  # type: ignore
                    coin=coin, sz=size_coin,
                    slippage=CFG.live_max_slippage_bps / 10_000.0,
                ),
            )
            if order_result.get("status") != "ok":
                raise RuntimeError(str(order_result))
            statuses = order_result.get("response", {}).get("data", {}).get("statuses", [{}])
            exit_fill = float(statuses[0].get("filled", {}).get("avgPx", exit_px_intended))
        except Exception as e:
            await log_activity("error", "exec", f"live close failed: {e}")
            return None

        ret = (exit_fill - entry_fill_px) / entry_fill_px if entry_fill_px else 0
        if direction == "short":
            ret = -ret
        pnl = size_usd * ret

        async with session_scope() as s:
            tr2 = await s.get(LiveTrade, trade_id)
            tr2.exit_px = exit_fill
            tr2.exit_reason = reason
            tr2.pnl_usd = pnl
            tr2.closed_ms = int(time.time() * 1000)
            tr2.status = "closed"

        await log_activity(
            "info" if pnl >= 0 else "warn", "exec",
            f"LIVE CLOSE {strategy} {coin} reason={reason} PnL=${pnl:+.2f}",
        )
        return pnl

    async def reconcile_with_exchange(self) -> dict:
        """Compare DB open live_trades with HL positions. Returns a diff summary.

        On significant mismatch, pauses the bot via BotState. Best-effort —
        if the API call fails, just logs.
        """
        if not self._capable:
            return {"ok": False, "reason": "not capable"}
        try:
            from sqlalchemy import select
            loop = asyncio.get_running_loop()
            user_state = await loop.run_in_executor(
                None,
                lambda: self._info.user_state(CFG.hl_account_address),  # type: ignore
            )
            # HL returns assetPositions: [{position: {coin, szi, entryPx, ...}}]
            positions = {}
            for ap in user_state.get("assetPositions", []):
                p = ap.get("position", {})
                coin = p.get("coin")
                szi = float(p.get("szi", 0))
                if coin and abs(szi) > 1e-9:
                    positions[coin] = szi

            async with session_scope() as s:
                res = await s.execute(
                    select(LiveTrade).where(LiveTrade.status == "open")
                )
                db_open = list(res.scalars().all())

            # Group DB by coin, signed
            db_by_coin: dict[str, float] = {}
            for tr in db_open:
                size = (tr.size_usd / tr.entry_fill_px) if tr.entry_fill_px else 0
                signed = size if tr.direction == "long" else -size
                db_by_coin[tr.coin] = db_by_coin.get(tr.coin, 0) + signed

            mismatches = []
            all_coins = set(positions) | set(db_by_coin)
            for coin in all_coins:
                hl_size = positions.get(coin, 0)
                db_size = db_by_coin.get(coin, 0)
                if abs(hl_size - db_size) > max(0.01, abs(db_size) * 0.05):
                    mismatches.append({
                        "coin": coin, "hl_size": hl_size, "db_size": db_size,
                    })

            if mismatches:
                await log_activity(
                    "error", "exec",
                    f"reconciliation mismatch: {mismatches}",
                )
                await update_state(
                    is_paused=True, live_armed=False,
                    pause_reason=f"reconciliation mismatch: {len(mismatches)} coin(s)",
                )
                return {"ok": False, "mismatches": mismatches}
            return {"ok": True, "n_positions": len(positions), "n_db_open": len(db_open)}
        except Exception as e:
            log.warning("reconciliation failed: %s", e)
            return {"ok": False, "reason": str(e)[:200]}
