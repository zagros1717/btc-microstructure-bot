"""
Hyperliquid WebSocket listener.

Subscribes to:
- `trades` for every coin in CFG.coins
- `l2Book` ONLY for BTC (for wall detection)

Key responsibilities beyond reconnect:

1. LIQUIDATION DETECTION VALIDATION
   ----------------------------------
   Hyperliquid's public `trades` channel may or may not include a
   `liquidation` field per trade. The exact shape is uncertain (different
   docs/SDK versions disagree). This listener:
   - tries multiple known shapes
   - increments diagnostic counters
   - exposes them via /api/feed_health
   - if RAW_WS_LOG=true, writes sampled raw messages to the DB

   If after the first hour no liquidation-flagged trades are seen, the
   strategy is auto-disabled (orchestrator handles this) with a warning.
   This avoids a silent failure where liquidation_fade runs forever with
   zero signal candidates.

2. RECONNECT
   ----------
   Exponential backoff (1s -> 60s) with heartbeat watchdog (force-close if no
   message for HEARTBEAT_TIMEOUT_S).

3. RAW SAMPLING
   ------------
   When CFG.raw_ws_log: every Nth message of each channel is persisted with
   payload, plus EVERY message that contains a liquidation-shaped field
   (regardless of sample rate). Crucial for first-week feed validation.
"""
from __future__ import annotations
import asyncio
import json
import logging
import time
from typing import Awaitable, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from .market_state import MarketState, TradeTick, L2Snapshot, L2Level
from .parsers import detect_liquidation_field as _detect_liquidation_field
from ..config import CFG
from ..db import (
    log_activity, update_state, LiquidationEvent, session_scope,
    record_raw_ws_sample,
)

log = logging.getLogger(__name__)

LiqCallback = Callable[[TradeTick, str], Awaitable[None]]
L2Callback = Callable[[str, L2Snapshot], Awaitable[None]]


class HyperliquidListener:
    def __init__(
        self,
        market: MarketState,
        on_liquidation: Optional[LiqCallback] = None,
        on_l2_update: Optional[L2Callback] = None,
    ):
        self.market = market
        self.on_liquidation = on_liquidation
        self.on_l2_update = on_l2_update
        self._stop = asyncio.Event()
        self._ws_alive = False
        self._last_msg_ms = 0

        # Diagnostic counters (exposed via /api/feed_health)
        self.counters = {
            "trades_total": 0,
            "trades_with_liq_field": 0,
            "trades_liq_shape_dict": 0,
            "trades_liq_shape_bool": 0,
            "trades_liq_shape_other": 0,
            "trades_parse_errors": 0,
            "l2_total": 0,
            "l2_parse_errors": 0,
            "unknown_channel": 0,
            "subscription_errors": 0,
            "reconnects": 0,
        }
        self._sample_counter: dict[str, int] = {}
        # Set true after the first valid liquidation-flagged trade is seen.
        # Triggers a one-time DB update to liq_feed_status = "validated".
        self._feed_validated_marked = False

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._connect_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("WS error: %s", e)
                await log_activity("warn", "ws", f"connection error: {str(e)[:200]}")

            if self._stop.is_set():
                break
            wait = min(backoff, 60.0)
            log.info("reconnecting in %.1fs", wait)
            await asyncio.sleep(wait)
            backoff = min(backoff * 2, 60.0)
            self.counters["reconnects"] += 1

    async def _connect_once(self) -> None:
        url = CFG.hl_ws_url
        async with websockets.connect(
            url, ping_interval=20, ping_timeout=10, max_size=4 * 1024 * 1024,
        ) as ws:
            self._ws_alive = True
            self._last_msg_ms = int(time.time() * 1000)
            await update_state(ws_connected=True, last_ws_msg_ms=self._last_msg_ms)
            await log_activity("info", "ws", "connected to Hyperliquid")

            for coin in CFG.coins:
                await ws.send(json.dumps({
                    "method": "subscribe",
                    "subscription": {"type": "trades", "coin": coin},
                }))
            await ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "l2Book", "coin": "BTC", "nSigFigs": 5},
            }))

            heartbeat_task = asyncio.create_task(self._heartbeat_watcher(ws))
            try:
                async for raw in ws:
                    self._last_msg_ms = int(time.time() * 1000)
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        self.counters["trades_parse_errors"] += 1
                        if CFG.raw_ws_log:
                            await record_raw_ws_sample(
                                "parse_error", {"raw": raw[:500]},
                                note="JSON parse failed",
                            )
                        continue
                    await self._handle_message(msg)
            except ConnectionClosed as e:
                log.info("WS closed: code=%s reason=%s", e.code, e.reason)
            finally:
                heartbeat_task.cancel()
                self._ws_alive = False
                await update_state(ws_connected=False)
                await log_activity("warn", "ws", "disconnected")

    async def _heartbeat_watcher(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(5)
                age_s = (int(time.time() * 1000) - self._last_msg_ms) / 1000.0
                if age_s > CFG.heartbeat_timeout_s:
                    log.warning("no WS message for %.1fs, forcing reconnect", age_s)
                    await log_activity("warn", "ws", f"heartbeat timeout {age_s:.0f}s, reconnecting")
                    try:
                        await ws.close(code=1000, reason="heartbeat")
                    except Exception:
                        pass
                    return
        except asyncio.CancelledError:
            pass

    async def _handle_message(self, msg: dict) -> None:
        ch = msg.get("channel")
        if ch == "trades":
            await self._handle_trades(msg.get("data") or [])
        elif ch == "l2Book":
            await self._handle_l2(msg.get("data") or {})
        elif ch == "subscriptionResponse":
            return
        elif ch in ("pong", "ping"):
            return
        elif ch == "error":
            self.counters["subscription_errors"] += 1
            if CFG.raw_ws_log:
                await record_raw_ws_sample("error", msg, note="server-side error")
            await log_activity("error", "ws", f"server error: {str(msg)[:200]}")
        else:
            self.counters["unknown_channel"] += 1
            if CFG.raw_ws_log and self.counters["unknown_channel"] <= 20:
                await record_raw_ws_sample(
                    "unknown", msg,
                    note=f"unknown channel: {ch}",
                )

    async def _handle_trades(self, data: list) -> None:
        for tr in data:
            try:
                coin = tr.get("coin")
                if coin not in self.market.trades:
                    continue
                px = float(tr.get("px"))
                sz = float(tr.get("sz"))
                if px <= 0 or sz <= 0:
                    continue
                ts_ms = int(tr.get("time") or time.time() * 1000)
                aggressor_buy = tr.get("side") == "B"

                # Liquidation detection — record diagnostics regardless of shape
                liq = _detect_liquidation_field(tr)
                is_liq = liq is not None

                self.counters["trades_total"] += 1
                if is_liq:
                    self.counters["trades_with_liq_field"] += 1
                    raw_field = tr.get("liquidation")
                    if isinstance(raw_field, dict):
                        self.counters["trades_liq_shape_dict"] += 1
                    elif isinstance(raw_field, bool):
                        self.counters["trades_liq_shape_bool"] += 1
                    else:
                        self.counters["trades_liq_shape_other"] += 1

                liq_user = liq.get("liquidatedUser") if is_liq else None
                liq_method = liq.get("method") if is_liq else None
                liquidated_side = None
                if is_liq:
                    # aggressor B = liquidator bought = closed a short -> SHORT liquidated
                    # aggressor A = liquidator sold = closed a long -> LONG liquidated
                    liquidated_side = "short" if aggressor_buy else "long"

                tick = TradeTick(
                    ts_ms=ts_ms, px=px, size=sz, size_usd=px * sz,
                    aggressor_buy=aggressor_buy, is_liq=is_liq,
                    liq_user=liq_user, liq_method=liq_method,
                )
                self.market.add_trade(coin, tick)

                # Sample raw trades for first-week validation
                if CFG.raw_ws_log:
                    n = self._sample_counter.get(coin, 0) + 1
                    self._sample_counter[coin] = n
                    # Always sample liquidation-flagged; periodic otherwise
                    if is_liq or n % CFG.raw_ws_sample_every == 0:
                        await record_raw_ws_sample(
                            "trades", tr, coin=coin, has_liquidation=is_liq,
                            note=("LIQUIDATION sample" if is_liq else "periodic sample"),
                        )

                if is_liq:
                    # On first valid liquidation, mark the feed as validated.
                    # Do this once per process — _feed_validated_marked guards.
                    if not self._feed_validated_marked:
                        self._feed_validated_marked = True
                        try:
                            await update_state(
                                liq_feed_status="validated",
                                liq_feed_validated_ms=int(time.time() * 1000),
                            )
                            await log_activity(
                                "info", "ws",
                                f"liquidation feed VALIDATED — first liq trade seen "
                                f"on {coin} (${px * sz:,.0f})",
                            )
                        except Exception as e:
                            log.warning("liq_feed_status update failed: %s", e)

                    try:
                        async with session_scope() as s:
                            s.add(LiquidationEvent(
                                coin=coin, ts_ms=ts_ms, px=px,
                                size_base=sz, size_usd=px * sz,
                                aggressor_side="B" if aggressor_buy else "A",
                                liquidated_side=liquidated_side or "",
                                method=liq_method,
                                raw=None,
                            ))
                    except Exception as e:
                        log.warning("liq persist failed: %s", e)

                    if self.on_liquidation:
                        try:
                            await self.on_liquidation(tick, coin)
                        except Exception as e:
                            log.exception("on_liquidation callback failed: %s", e)
            except Exception as e:
                self.counters["trades_parse_errors"] += 1
                log.warning("trade parse failed: %s; msg=%s", e, str(tr)[:200])

    async def _handle_l2(self, data: dict) -> None:
        coin = data.get("coin")
        if coin != "BTC":
            return
        levels = data.get("levels") or [[], []]
        if len(levels) != 2:
            self.counters["l2_parse_errors"] += 1
            return
        try:
            ts_ms = int(data.get("time") or time.time() * 1000)
            bids_raw, asks_raw = levels
            bids = [
                L2Level(px=float(l["px"]), size=float(l["sz"]),
                        size_usd=float(l["px"]) * float(l["sz"]))
                for l in bids_raw if l.get("px") and l.get("sz")
            ]
            asks = [
                L2Level(px=float(l["px"]), size=float(l["sz"]),
                        size_usd=float(l["px"]) * float(l["sz"]))
                for l in asks_raw if l.get("px") and l.get("sz")
            ]
            bids.sort(key=lambda x: -x.px)
            asks.sort(key=lambda x: x.px)
            snap = L2Snapshot(ts_ms=ts_ms, bids=bids, asks=asks)
            self.market.update_l2(coin, snap)
            self.counters["l2_total"] += 1

            if CFG.raw_ws_log and self.counters["l2_total"] % CFG.raw_ws_sample_every == 0:
                await record_raw_ws_sample(
                    "l2Book",
                    {"coin": coin, "time": ts_ms, "n_bids": len(bids),
                     "n_asks": len(asks),
                     "top_bid": bids[0].px if bids else None,
                     "top_ask": asks[0].px if asks else None},
                    coin=coin, note="periodic l2 sample",
                )

            if self.on_l2_update:
                try:
                    await self.on_l2_update(coin, snap)
                except Exception as e:
                    log.exception("on_l2_update callback failed: %s", e)
        except Exception as e:
            self.counters["l2_parse_errors"] += 1
            log.warning("l2 parse failed: %s", e)
