"""
FastAPI server.

Public read endpoints (no auth required):
  GET  /                       → dashboard
  GET  /health
  GET  /api/status
  GET  /api/activity
  GET  /api/trades
  GET  /api/signals
  GET  /api/walls
  GET  /api/reject_stats
  GET  /api/feed_health
  GET  /api/report/daily

Authenticated control endpoints (Bearer token):
  POST /api/toggle              → {strategy: "liq|wall|live_armed", enabled: bool}
  POST /api/pause / resume
  POST /api/emergency_stop
  POST /api/clear_pause         → clear pause flag without resuming strategies

Auth: Authorization: Bearer <API_TOKEN> where API_TOKEN matches CFG.api_token.
The default token "change-me-please" is REJECTED — startup also warns.

CORS: allow_origins is read from API_ALLOWED_ORIGINS env (comma-separated).
Default is the same-origin (no wildcards). Use API_ALLOWED_ORIGINS=* only
for local dev.

NOTE: Strategy toggles write to BotState in DB, NEVER mutate Config.
The frozen Config is the static startup baseline; runtime state lives in DB.
"""
from __future__ import annotations
import os
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select, desc, func, and_

from ..config import CFG
from ..db import (
    session_scope, BotState, ActivityLog, Signal, PaperTrade, LiveTrade,
    SignalRejectStat, WallEvent,
    update_state, log_activity, get_state,
)

log = logging.getLogger(__name__)

_state = {"orchestrator": None}


def _check_auth(authorization: Optional[str]) -> None:
    if CFG.api_token == "change-me-please":
        raise HTTPException(503, "API_TOKEN not set; control endpoints disabled")
    if not authorization:
        raise HTTPException(401, "missing Authorization header")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(401, "expected: Bearer <token>")
    # Constant-time compare
    import hmac
    if not hmac.compare_digest(parts[1], CFG.api_token):
        raise HTTPException(403, "invalid token")


def _allowed_origins() -> list[str]:
    raw = os.getenv("API_ALLOWED_ORIGINS", "").strip()
    if not raw:
        # Conservative default: only same-origin via the served HTML.
        # FastAPI doesn't need CORS for same-origin requests, so [] is fine.
        return []
    return [o.strip() for o in raw.split(",") if o.strip()]


def create_app() -> FastAPI:
    app = FastAPI(title="BTC Microstructure Bot", version="0.2.0")
    origins = _allowed_origins()
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
            allow_credentials=False,
        )

    frontend_dir = Path(__file__).resolve().parents[2] / "frontend"

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "ts_ms": int(time.time() * 1000)}

    @app.get("/")
    async def root() -> FileResponse:
        idx = frontend_dir / "index.html"
        if idx.exists():
            return FileResponse(str(idx))
        return JSONResponse({"hint": "frontend/index.html missing"}, status_code=404)

    @app.get("/app.jsx")
    async def app_js() -> FileResponse:
        f = frontend_dir / "app.jsx"
        if f.exists():
            return FileResponse(str(f), media_type="application/javascript")
        raise HTTPException(404)

    @app.get("/style.css")
    async def style_css() -> FileResponse:
        f = frontend_dir / "style.css"
        if f.exists():
            return FileResponse(str(f), media_type="text/css")
        raise HTTPException(404)

    # ── status ──────────────────────────────────────────────────────────
    @app.get("/api/status")
    async def status() -> dict:
        async with session_scope() as s:
            st = await s.get(BotState, 1)
            open_paper = (await s.execute(
                select(func.count(PaperTrade.id)).where(PaperTrade.status == "open")
            )).scalar() or 0
            open_live = (await s.execute(
                select(func.count(LiveTrade.id)).where(LiveTrade.status == "open")
            )).scalar() or 0
            cutoff = int((time.time() - 24 * 3600) * 1000)
            today_signals = (await s.execute(
                select(func.count(Signal.id)).where(Signal.ts_ms >= cutoff)
            )).scalar() or 0
            today_accepted = (await s.execute(
                select(func.count(Signal.id)).where(and_(
                    Signal.ts_ms >= cutoff, Signal.accepted == True,  # noqa: E712
                ))
            )).scalar() or 0

        now_ms = int(time.time() * 1000)
        ws_age_s = (now_ms - (st.last_ws_msg_ms or 0)) / 1000.0 if st and st.last_ws_msg_ms else None
        hb_age_s = (now_ms - (st.last_heartbeat_ms or 0)) / 1000.0 if st and st.last_heartbeat_ms else None

        live_capable = False
        live_init_error = None
        orch = _state.get("orchestrator")
        if orch is not None:
            live_capable = orch.live.capable
            live_init_error = orch.live.init_error

        return {
            "ts_ms": now_ms,
            "config": {
                "coins": CFG.coins,
                "live_code_enabled": CFG.live_code_enabled,
                "live_capable": live_capable,
                "live_init_error": live_init_error,
                "require_protected_exits": CFG.require_protected_exits,
                "paper_starting_balance_usd": CFG.paper_starting_balance_usd,
                "max_concurrent_positions": CFG.max_concurrent_positions,
                "max_positions_per_strategy": CFG.max_positions_per_strategy,
                "max_positions_per_coin": CFG.max_positions_per_coin,
                "hardcoded_max_position_usd": CFG.hardcoded_max_position_usd,
                "raw_ws_log": CFG.raw_ws_log,
                "api_token_default": CFG.api_token == "change-me-please",
            },
            "runtime": {
                "liq_fade_enabled": st.liq_fade_enabled if st else True,
                "wall_enabled": st.wall_enabled if st else True,
                "live_armed": st.live_armed if st else False,
                "liq_feed_status": st.liq_feed_status if st else "unknown",
                "liq_feed_validated_ms": st.liq_feed_validated_ms if st else 0,
                "liq_feed_checked_ms": st.liq_feed_checked_ms if st else 0,
                # Effective: can liq_fade actually run right now?
                "liq_fade_effective": (
                    bool(st and st.liq_fade_enabled
                         and st.liq_feed_status != "unavailable")
                ),
            },
            "state": {
                "is_paused": st.is_paused if st else False,
                "pause_reason": st.pause_reason if st else None,
                "paper_equity_usd": st.paper_equity_usd if st else CFG.paper_starting_balance_usd,
                "paper_peak_equity_usd": st.paper_peak_equity_usd if st else CFG.paper_starting_balance_usd,
                "paper_realized_pnl_usd": st.paper_realized_pnl_usd if st else 0.0,
                "daily_pnl_usd": st.daily_pnl_usd if st else 0.0,
                "daily_pnl_date": st.daily_pnl_date if st else None,
                "consecutive_losses": st.consecutive_losses if st else 0,
                "ws_connected": st.ws_connected if st else False,
                "ws_age_s": ws_age_s,
                "heartbeat_age_s": hb_age_s,
            },
            "stats": {
                "open_paper_trades": open_paper,
                "open_live_trades": open_live,
                "signals_24h": today_signals,
                "accepted_signals_24h": today_accepted,
            },
        }

    # ── feed health (diagnostic) ────────────────────────────────────────
    @app.get("/api/feed_health")
    async def feed_health() -> dict:
        orch = _state.get("orchestrator")
        if orch is None:
            return {"ok": False, "reason": "orchestrator not started"}
        c = orch.listener.counters
        per_coin_age = {}
        now_ms = int(time.time() * 1000)
        for coin, ts in orch.market.last_trade_ms.items():
            per_coin_age[coin] = (now_ms - ts) / 1000.0 if ts else None
        return {
            "counters": dict(c),
            "per_coin_last_trade_age_s": per_coin_age,
            "validation_window_s": 3600,
            "raw_ws_log_enabled": CFG.raw_ws_log,
        }

    # ── activity ────────────────────────────────────────────────────────
    @app.get("/api/activity")
    async def activity(limit: int = Query(50, ge=1, le=500)) -> dict:
        async with session_scope() as s:
            res = await s.execute(
                select(ActivityLog).order_by(desc(ActivityLog.id)).limit(limit)
            )
            rows = list(res.scalars().all())
        return {"items": [
            {"id": r.id, "ts_ms": r.ts_ms, "level": r.level,
             "category": r.category, "message": r.message, "meta": r.meta}
            for r in rows
        ]}

    # ── trades ──────────────────────────────────────────────────────────
    @app.get("/api/trades")
    async def trades(
        status: Optional[str] = Query(None),
        limit: int = Query(100, ge=1, le=500),
    ) -> dict:
        async with session_scope() as s:
            q = select(PaperTrade).order_by(desc(PaperTrade.id)).limit(limit)
            if status:
                q = select(PaperTrade).where(PaperTrade.status == status).order_by(desc(PaperTrade.id)).limit(limit)
            res = await s.execute(q)
            rows = list(res.scalars().all())

            live_q = select(LiveTrade).order_by(desc(LiveTrade.id)).limit(limit)
            if status:
                live_q = select(LiveTrade).where(LiveTrade.status == status).order_by(desc(LiveTrade.id)).limit(limit)
            live_res = await s.execute(live_q)
            live_rows = list(live_res.scalars().all())

        def serialize_paper(r: PaperTrade) -> dict:
            return {
                "id": r.id, "kind": "paper", "coin": r.coin, "strategy": r.strategy,
                "direction": r.direction, "size_usd": r.size_usd,
                "entry_px": r.entry_px, "entry_fill_px": r.entry_fill_px,
                "stop_px": r.stop_px, "target_px": r.target_px,
                "exit_px": r.exit_px, "exit_reason": r.exit_reason,
                "pnl_usd": r.pnl_usd, "fees_usd": r.fees_usd,
                "mae_usd": r.mae_usd, "mfe_usd": r.mfe_usd,
                "opened_ms": r.opened_ms, "closed_ms": r.closed_ms,
                "status": r.status,
            }

        def serialize_live(r: LiveTrade) -> dict:
            return {
                "id": r.id, "kind": "live", "coin": r.coin, "strategy": r.strategy,
                "direction": r.direction, "size_usd": r.size_usd,
                "entry_px": r.entry_px, "entry_fill_px": r.entry_fill_px,
                "stop_px": r.stop_px, "target_px": r.target_px,
                "exit_px": r.exit_px, "exit_reason": r.exit_reason,
                "pnl_usd": r.pnl_usd,
                "mae_usd": r.mae_usd, "mfe_usd": r.mfe_usd,
                "opened_ms": r.opened_ms, "closed_ms": r.closed_ms,
                "status": r.status, "error": r.error,
                "hl_stop_oid": r.hl_stop_oid, "hl_target_oid": r.hl_target_oid,
            }

        return {
            "paper": [serialize_paper(r) for r in rows],
            "live": [serialize_live(r) for r in live_rows],
        }

    # ── signals ─────────────────────────────────────────────────────────
    @app.get("/api/signals")
    async def signals(limit: int = Query(50, ge=1, le=500)) -> dict:
        async with session_scope() as s:
            res = await s.execute(select(Signal).order_by(desc(Signal.id)).limit(limit))
            rows = list(res.scalars().all())
        return {"items": [
            {"id": r.id, "ts_ms": r.ts_ms, "coin": r.coin, "strategy": r.strategy,
             "direction": r.direction, "accepted": r.accepted, "reject_reason": r.reject_reason,
             "entry_px": r.entry_px, "stop_px": r.stop_px, "target_px": r.target_px,
             "size_usd": r.size_usd, "meta": r.meta}
            for r in rows
        ]}

    # ── reject stats (aggregated) ───────────────────────────────────────
    @app.get("/api/reject_stats")
    async def reject_stats(hours: int = Query(24, ge=1, le=168)) -> dict:
        cutoff_hour = int(time.time() // 3600) - hours
        async with session_scope() as s:
            res = await s.execute(
                select(SignalRejectStat).where(SignalRejectStat.hour_bucket >= cutoff_hour)
                .order_by(desc(SignalRejectStat.count)).limit(500)
            )
            rows = list(res.scalars().all())
        return {"items": [
            {"strategy": r.strategy, "coin": r.coin,
             "reason_short": r.reason_short, "count": r.count,
             "hour_bucket": r.hour_bucket}
            for r in rows
        ]}

    # ── walls ───────────────────────────────────────────────────────────
    @app.get("/api/walls")
    async def walls() -> dict:
        orch = _state.get("orchestrator")
        if orch is None:
            return {"items": []}
        items = []
        for w in orch.wall_strategy.walls.values():
            items.append({
                "wall_id": w.wall_id, "side": w.side, "px": w.px,
                "size_usd_initial": w.size_usd_initial,
                "size_usd_current": w.size_usd_current,
                "size_usd_min": w.size_usd_min,
                "detected_ms": w.detected_ms,
                "confirmed_ms": w.confirmed_ms, "state": w.state,
            })
        return {"items": items, "count": len(items)}

    # ── daily report ────────────────────────────────────────────────────
    @app.get("/api/report/daily")
    async def report_daily(days: int = Query(1, ge=1, le=30)) -> dict:
        cutoff_ms = int((time.time() - days * 24 * 3600) * 1000)
        async with session_scope() as s:
            res = await s.execute(
                select(PaperTrade).where(and_(
                    PaperTrade.status == "closed",
                    PaperTrade.closed_ms >= cutoff_ms,
                ))
            )
            trades = list(res.scalars().all())

        if not trades:
            return {
                "window_days": days,
                "n_trades": 0,
                "by_strategy": {},
                "by_coin": {},
                "overall": _empty_summary(),
            }

        overall = _summarize(trades)
        by_strategy = {}
        by_coin = {}
        for tr in trades:
            by_strategy.setdefault(tr.strategy, []).append(tr)
            by_coin.setdefault(tr.coin, []).append(tr)
        return {
            "window_days": days,
            "n_trades": len(trades),
            "overall": overall,
            "by_strategy": {k: _summarize(v) for k, v in by_strategy.items()},
            "by_coin": {k: _summarize(v) for k, v in by_coin.items()},
        }

    # ── controls (auth required) ────────────────────────────────────────
    @app.post("/api/toggle")
    async def toggle(payload: dict, authorization: Optional[str] = Header(None)) -> dict:
        _check_auth(authorization)
        target = payload.get("strategy")
        enabled = bool(payload.get("enabled"))
        force = bool(payload.get("force", False))
        if target not in ("liq", "wall", "live_armed"):
            raise HTTPException(400, "strategy must be liq, wall, or live_armed")

        if target == "liq":
            if enabled:
                # Don't let operator turn liq_fade ON if feed is known-unavailable
                # without an explicit force=true acknowledgement
                async with session_scope() as s:
                    st = await s.get(BotState, 1)
                    feed_status = st.liq_feed_status if st else "unknown"
                if feed_status == "unavailable" and not force:
                    raise HTTPException(
                        409,
                        "liq feed status is 'unavailable' (no liquidation data "
                        "detected). Enabling liq_fade now is unsafe. "
                        "Inspect raw_ws_samples to verify feed shape, then "
                        "either fix the parser or POST again with force=true "
                        "to override (which also clears feed status to "
                        "'validated')."
                    )
                if feed_status == "unavailable" and force:
                    # Operator explicitly overriding — also clear feed status
                    await update_state(
                        liq_fade_enabled=True,
                        liq_feed_status="validated",
                    )
                    await log_activity(
                        "warn", "system",
                        "liq_fade force-enabled with feed status overridden "
                        "to 'validated' by operator via API",
                    )
                    return {"ok": True, "strategy": target, "enabled": True, "forced": True}
            await update_state(liq_fade_enabled=enabled)
        elif target == "wall":
            await update_state(wall_enabled=enabled)
        elif target == "live_armed":
            # Extra safety on arming
            if enabled:
                orch = _state.get("orchestrator")
                if orch is None or not orch.live.capable:
                    raise HTTPException(
                        400,
                        f"cannot arm: SDK not capable "
                        f"(error={orch.live.init_error if orch else 'no orch'})"
                    )
                if CFG.require_protected_exits:
                    raise HTTPException(
                        400,
                        "cannot arm: REQUIRE_PROTECTED_EXITS=true and exchange-side "
                        "stops are not yet implemented. Live orders would be refused. "
                        "Implement protected exits or set REQUIRE_PROTECTED_EXITS=false "
                        "(NOT recommended for production).",
                    )
            await update_state(live_armed=enabled)

        await log_activity(
            "warn", "system",
            f"toggle {target} -> {'on' if enabled else 'off'} via API",
        )
        return {"ok": True, "strategy": target, "enabled": enabled}

    # ── liq_feed override (operator force-clears unavailable status) ─────
    @app.post("/api/liq_feed/override")
    async def liq_feed_override(
        payload: dict, authorization: Optional[str] = Header(None),
    ) -> dict:
        """Force-clear liq_feed_status from 'unavailable' to a chosen state.

        Body: {status: "validated"|"unknown"}

        Use case: after inspecting raw_ws_samples, operator confirmed the
        feed is fine (or fixed the parser) and wants to re-enable liq_fade.
        """
        _check_auth(authorization)
        new_status = payload.get("status", "validated")
        if new_status not in ("validated", "unknown"):
            raise HTTPException(400, "status must be 'validated' or 'unknown'")
        await update_state(liq_feed_status=new_status)
        await log_activity(
            "warn", "system",
            f"liq_feed_status manually set to '{new_status}' via API override",
        )
        return {"ok": True, "liq_feed_status": new_status}

    @app.post("/api/pause")
    async def pause(authorization: Optional[str] = Header(None)) -> dict:
        _check_auth(authorization)
        await update_state(is_paused=True, pause_reason="manual")
        await log_activity("warn", "system", "manually paused via API")
        return {"ok": True, "is_paused": True}

    @app.post("/api/resume")
    async def resume(authorization: Optional[str] = Header(None)) -> dict:
        _check_auth(authorization)
        # Resume also resets consecutive losses (assume operator inspected)
        await update_state(is_paused=False, pause_reason=None, consecutive_losses=0)
        await log_activity("info", "system", "manually resumed via API (consec losses reset)")
        return {"ok": True, "is_paused": False}

    @app.post("/api/clear_pause")
    async def clear_pause(authorization: Optional[str] = Header(None)) -> dict:
        _check_auth(authorization)
        await update_state(pause_reason=None)
        return {"ok": True}

    @app.post("/api/emergency_stop")
    async def emergency(authorization: Optional[str] = Header(None)) -> dict:
        _check_auth(authorization)
        orch = _state.get("orchestrator")
        if orch is None:
            raise HTTPException(503, "orchestrator not available")
        # Immediately disarm live first
        await update_state(
            is_paused=True, live_armed=False,
            pause_reason="EMERGENCY STOP via API",
        )
        result = await orch.manager.emergency_close_all("emergency_stop")
        await log_activity("error", "system", f"EMERGENCY STOP — closed {result}")
        return {"ok": True, "closed": result}

    return app


def _empty_summary() -> dict:
    return {
        "n": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
        "gross_pnl_usd": 0.0, "fees_usd": 0.0, "net_pnl_usd": 0.0,
        "avg_win_usd": 0.0, "avg_loss_usd": 0.0,
        "profit_factor": 0.0, "max_drawdown_usd": 0.0,
        "avg_mae_usd": 0.0, "avg_mfe_usd": 0.0,
    }


def _summarize(trades: list) -> dict:
    """Compute summary statistics on a list of closed PaperTrade objects."""
    if not trades:
        return _empty_summary()
    n = len(trades)
    wins = [t for t in trades if (t.pnl_usd or 0) > 0]
    losses = [t for t in trades if (t.pnl_usd or 0) < 0]
    pnls = [t.pnl_usd or 0 for t in trades]
    fees = sum(t.fees_usd or 0 for t in trades)
    net = sum(pnls)
    gross = net + fees

    sum_win = sum(t.pnl_usd or 0 for t in wins)
    sum_loss = -sum(t.pnl_usd or 0 for t in losses)  # positive number

    # Equity-curve drawdown over this window
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.closed_ms or 0):
        running += t.pnl_usd or 0
        peak = max(peak, running)
        dd = peak - running
        if dd > max_dd:
            max_dd = dd

    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / n if n else 0.0,
        "gross_pnl_usd": float(gross),
        "fees_usd": float(fees),
        "net_pnl_usd": float(net),
        "avg_win_usd": float(sum_win / len(wins)) if wins else 0.0,
        "avg_loss_usd": float(sum_loss / len(losses)) if losses else 0.0,
        "profit_factor": float(sum_win / sum_loss) if sum_loss > 0 else (float("inf") if sum_win > 0 else 0.0),
        "max_drawdown_usd": float(max_dd),
        "avg_mae_usd": float(sum(t.mae_usd or 0 for t in trades) / n),
        "avg_mfe_usd": float(sum(t.mfe_usd or 0 for t in trades) / n),
    }


def attach_orchestrator(orch) -> None:
    _state["orchestrator"] = orch
