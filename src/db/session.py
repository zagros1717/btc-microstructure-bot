"""
Async SQLAlchemy session factory + lightweight helpers.
"""
from __future__ import annotations
import re
import time
import logging
from contextlib import asynccontextmanager
from typing import Optional, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import select, update

from .models import Base, BotState, ActivityLog, SignalRejectStat, RawWsSample
from ..config import CFG

log = logging.getLogger(__name__)

engine = create_async_engine(
    CFG.database_url,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=300,
    echo=False,
)

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Use as: `async with session_scope() as s: ...`"""
    s = SessionLocal()
    try:
        yield s
        await s.commit()
    except Exception:
        await s.rollback()
        raise
    finally:
        await s.close()


async def init_db() -> None:
    """Create all tables if missing. Safe to call repeatedly.

    Critical fail-safes applied on EVERY startup, regardless of prior DB state:
      - live_armed is force-reset to False
      - liq_feed_status is reset to "unknown" (we re-validate the feed each run)
      - is_paused is preserved (operator may have paused intentionally before restart)

    The reasoning: a crash, restart, or redeploy must never resume sending
    live orders. Re-arming is always a deliberate manual step.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_scope() as s:
        existing = await s.get(BotState, 1)
        if existing is None:
            s.add(BotState(
                id=1,
                paper_equity_usd=CFG.paper_starting_balance_usd,
                paper_peak_equity_usd=CFG.paper_starting_balance_usd,
                liq_fade_enabled=CFG.enable_liquidation_fade,
                wall_enabled=CFG.enable_liquidity_wall,
                live_armed=False,
                liq_feed_status="unknown",
            ))
        else:
            # FAIL-SAFE: force live_armed off on every startup.
            # If operator had it on before crash, they must re-arm deliberately.
            was_armed = bool(existing.live_armed)
            existing.live_armed = False
            # Reset feed status so we re-validate from scratch each run
            existing.liq_feed_status = "unknown"
            existing.liq_feed_checked_ms = 0
            # Don't touch liq_feed_validated_ms — it's historical info
            if was_armed:
                # Best-effort log; we're inside the same session so use
                # plain logger here, log_activity will work after commit
                log.warning(
                    "STARTUP FAIL-SAFE: forced live_armed=False "
                    "(was True in DB before restart)"
                )
                # Also append an activity row in same transaction so the
                # operator sees this in the dashboard immediately.
                s.add(ActivityLog(
                    ts_ms=int(time.time() * 1000),
                    level="error",
                    category="system",
                    message="STARTUP FAIL-SAFE: live_armed reset to False on startup",
                ))


async def log_activity(level: str, category: str, message: str, meta: Optional[dict] = None) -> None:
    """Best-effort activity log write. Swallows errors so it never breaks callers."""
    try:
        async with session_scope() as s:
            s.add(ActivityLog(
                ts_ms=int(time.time() * 1000),
                level=level,
                category=category,
                message=message[:512],
                meta=meta,
            ))
    except Exception as e:
        log.warning("activity log write failed: %s", e)


async def get_state() -> BotState:
    async with session_scope() as s:
        st = await s.get(BotState, 1)
        if st is None:
            st = BotState(id=1)
            s.add(st)
        return st


async def update_state(**fields) -> None:
    async with session_scope() as s:
        await s.execute(update(BotState).where(BotState.id == 1).values(**fields))


async def prune_old_activity(keep_last_n: int = 5000) -> None:
    """Trim activity_log to last N rows. Call periodically."""
    from sqlalchemy import text
    try:
        async with session_scope() as s:
            await s.execute(text(
                "DELETE FROM activity_log WHERE id NOT IN "
                "(SELECT id FROM activity_log ORDER BY id DESC LIMIT :n)"
            ), {"n": keep_last_n})
            # Also prune raw_ws_samples (these can grow fast)
            await s.execute(text(
                "DELETE FROM raw_ws_samples WHERE id NOT IN "
                "(SELECT id FROM raw_ws_samples ORDER BY id DESC LIMIT :n)"
            ), {"n": keep_last_n})
    except Exception as e:
        log.warning("prune activity failed: %s", e)


# ── Reject-stat helpers ────────────────────────────────────────────────
_REASON_NUMERIC_PATTERN = re.compile(r"[-+]?\d*\.?\d+")


def _shorten_reason(reason: str) -> str:
    """Strip numeric details so 'cooldown (47s left)' and 'cooldown (12s left)'
    bucket together as 'cooldown ()'. Keeps text up to 64 chars."""
    if not reason:
        return ""
    # Replace all numbers with #
    short = _REASON_NUMERIC_PATTERN.sub("#", reason)
    return short[:64]


async def record_reject_stat(strategy: str, coin: str, reason: str) -> None:
    """Increment rolling counter for (strategy, coin, reason_short, current hour).
    Best-effort. Silently fails on errors."""
    try:
        from sqlalchemy import and_
        hour_bucket = int(time.time() // 3600)
        reason_short = _shorten_reason(reason)
        async with session_scope() as s:
            res = await s.execute(
                select(SignalRejectStat).where(and_(
                    SignalRejectStat.hour_bucket == hour_bucket,
                    SignalRejectStat.strategy == strategy,
                    SignalRejectStat.coin == coin,
                    SignalRejectStat.reason_short == reason_short,
                ))
            )
            row = res.scalars().first()
            if row is not None:
                row.count = row.count + 1
            else:
                s.add(SignalRejectStat(
                    hour_bucket=hour_bucket, strategy=strategy, coin=coin,
                    reason_short=reason_short, count=1,
                ))
    except Exception as e:
        log.debug("reject stat write failed: %s", e)


async def record_raw_ws_sample(channel: str, payload: dict, *,
                                coin: Optional[str] = None,
                                has_liquidation: bool = False,
                                note: Optional[str] = None) -> None:
    """Persist a raw WS message sample. Best-effort. Sampling is the caller's
    responsibility (this writes every call)."""
    try:
        async with session_scope() as s:
            s.add(RawWsSample(
                ts_ms=int(time.time() * 1000),
                channel=channel[:32],
                coin=coin[:32] if coin else None,
                has_liquidation=has_liquidation,
                payload=payload,
                note=note[:256] if note else None,
            ))
    except Exception as e:
        log.debug("raw ws sample write failed: %s", e)
