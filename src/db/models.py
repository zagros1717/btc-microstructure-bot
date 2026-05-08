"""
Database schema. SQLAlchemy 2.0 async style.

Tables:
- liquidation_events: every liquidation-flagged trade (raw data)
- cascades: detected cascade events (aggregated liquidations)
- wall_events: detected liquidity walls + their lifecycle
- signals: every signal (filed or rejected, with reason)
- paper_trades: virtual trades (entry, exit, P&L)
- live_trades: real trades (only if live mode enabled)
- bot_state: singleton row tracking equity, daily P&L, drawdown
- activity_log: rolling activity feed for the dashboard
"""
from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, BigInteger, String, Float, Boolean, DateTime,
    Text, JSON, Index, ForeignKey
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from typing import Optional


def _utcnow() -> datetime:
    """Single source of truth for UTC timestamps. Replaces deprecated datetime.utcnow."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class LiquidationEvent(Base):
    __tablename__ = "liquidation_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    coin: Mapped[str] = mapped_column(String(32), index=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True)  # exchange time
    px: Mapped[float] = mapped_column(Float)
    size_base: Mapped[float] = mapped_column(Float)
    size_usd: Mapped[float] = mapped_column(Float)
    # Aggressor side of the trade that liquidated. 'B' = aggressor was buyer (i.e., shorts liquidated).
    # 'A' = aggressor was seller (i.e., longs liquidated).
    aggressor_side: Mapped[str] = mapped_column(String(1))
    # liquidatedUser side derived: 'long' (sold) or 'short' (bought)
    liquidated_side: Mapped[str] = mapped_column(String(8))
    method: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    raw: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        Index("ix_liq_coin_ts", "coin", "ts_ms"),
    )


class Cascade(Base):
    __tablename__ = "cascades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    coin: Mapped[str] = mapped_column(String(32), index=True)
    start_ms: Mapped[int] = mapped_column(BigInteger)
    end_ms: Mapped[int] = mapped_column(BigInteger)
    side: Mapped[str] = mapped_column(String(8))  # 'long' or 'short' (which side liquidated)
    total_usd: Mapped[float] = mapped_column(Float)
    n_events: Mapped[int] = mapped_column(Integer)
    px_start: Mapped[float] = mapped_column(Float)
    px_end: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class WallEvent(Base):
    """Lifecycle of a single tracked wall — persisted from detection to final state."""
    __tablename__ = "wall_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    coin: Mapped[str] = mapped_column(String(32), index=True)
    wall_id: Mapped[str] = mapped_column(String(48), index=True)  # in-memory id (side:px)
    side: Mapped[str] = mapped_column(String(4))  # 'bid' or 'ask'
    px: Mapped[float] = mapped_column(Float)
    size_usd_initial: Mapped[float] = mapped_column(Float)
    size_usd_min: Mapped[float] = mapped_column(Float)
    size_usd_final: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    detected_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    confirmed_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    final_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # final_state: tracking | confirmed | consumed | vanished | expired | fired
    final_state: Mapped[str] = mapped_column(String(16), default="tracking", index=True)
    fired_signal_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("signals.id"), nullable=True
    )
    notes: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    coin: Mapped[str] = mapped_column(String(32), index=True)
    strategy: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(8))  # 'long' or 'short'
    accepted: Mapped[bool] = mapped_column(Boolean)
    reject_reason: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    entry_px: Mapped[float] = mapped_column(Float)
    stop_px: Mapped[float] = mapped_column(Float)
    target_px: Mapped[float] = mapped_column(Float)
    size_usd: Mapped[float] = mapped_column(Float)
    meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class PaperTrade(Base):
    __tablename__ = "paper_trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[Optional[int]] = mapped_column(ForeignKey("signals.id"), nullable=True)
    coin: Mapped[str] = mapped_column(String(32), index=True)
    strategy: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(8))
    size_usd: Mapped[float] = mapped_column(Float)
    leverage: Mapped[float] = mapped_column(Float, default=1.0)
    entry_px: Mapped[float] = mapped_column(Float)
    entry_fill_px: Mapped[float] = mapped_column(Float)  # after slippage
    stop_px: Mapped[float] = mapped_column(Float)
    target_px: Mapped[float] = mapped_column(Float)
    exit_px: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    pnl_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fees_usd: Mapped[float] = mapped_column(Float, default=0.0)
    # MAE = max adverse excursion (worst loss seen during life of trade)
    # MFE = max favorable excursion (best profit seen during life of trade)
    # Both stored as USD pnl (not pct), updated each tick by TradeManager.
    mae_usd: Mapped[float] = mapped_column(Float, default=0.0)
    mfe_usd: Mapped[float] = mapped_column(Float, default=0.0)
    opened_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    closed_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(8), default="open", index=True)  # open|closed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class LiveTrade(Base):
    __tablename__ = "live_trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[Optional[int]] = mapped_column(ForeignKey("signals.id"), nullable=True)
    coin: Mapped[str] = mapped_column(String(32), index=True)
    strategy: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(8))
    size_usd: Mapped[float] = mapped_column(Float)
    entry_px: Mapped[float] = mapped_column(Float)
    entry_fill_px: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop_px: Mapped[float] = mapped_column(Float)
    target_px: Mapped[float] = mapped_column(Float)
    exit_px: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    pnl_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fees_usd: Mapped[float] = mapped_column(Float, default=0.0)
    mae_usd: Mapped[float] = mapped_column(Float, default=0.0)
    mfe_usd: Mapped[float] = mapped_column(Float, default=0.0)
    opened_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    closed_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    hl_order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Reduce-only protective orders placed exchange-side (oid strings).
    # If null, the position has NO exchange-side protection; client-side only.
    hl_stop_oid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    hl_target_oid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class BotState(Base):
    """Singleton (id=1). Tracks running stats AND runtime toggles.

    All runtime mutations of strategy on/off and live_armed go through this
    table — NEVER through mutating the frozen Config singleton.
    """
    __tablename__ = "bot_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    paper_equity_usd: Mapped[float] = mapped_column(Float, default=1000.0)
    paper_peak_equity_usd: Mapped[float] = mapped_column(Float, default=1000.0)
    paper_realized_pnl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    daily_pnl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    daily_pnl_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # YYYY-MM-DD
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_reason: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    last_heartbeat_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    ws_connected: Mapped[bool] = mapped_column(Boolean, default=False)
    last_ws_msg_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    # Runtime toggles (mutable via API). Initial values mirror startup CFG.
    liq_fade_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    wall_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # live_armed: even if SDK is initialized, must be true for orders to send.
    # ALWAYS forced to False on every process startup (fail-safe). Operators
    # must re-arm explicitly via the API after each restart. This means an
    # accidental crash + restart can never resume sending live orders.
    live_armed: Mapped[bool] = mapped_column(Boolean, default=False)
    # Liquidation feed health status:
    #   "unknown"     — bot just started, not enough data yet
    #   "validated"   — at least one liquidation-flagged trade has been seen
    #   "unavailable" — auto-disable: no liq trades seen after FEED_VALIDATION_AFTER_S
    # When status == "unavailable", liq_fade strategy will not run regardless
    # of liq_fade_enabled. Operator can override via /api/liq_feed/override.
    liq_feed_status: Mapped[str] = mapped_column(String(16), default="unknown")
    liq_feed_validated_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    liq_feed_checked_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class ActivityLog(Base):
    __tablename__ = "activity_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    level: Mapped[str] = mapped_column(String(8))  # info|warn|error
    category: Mapped[str] = mapped_column(String(24))  # ws|strategy|exec|risk|system
    message: Mapped[str] = mapped_column(String(512))
    meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        Index("ix_activity_ts", "ts_ms"),
    )


class SignalRejectStat(Base):
    """Aggregated per-strategy/per-coin/per-reason rejection counters.

    Persisting every rejection would explode the DB (strategies evaluate
    every 0.5s × 6 coins × 2 strats = 24 evals/sec). Instead we increment a
    rolling bucket per (strategy, coin, reason_short, hour_bucket).

    `reason_short` is the first ~32 chars of reject_reason without numeric
    detail (so "cooldown (47s left)" and "cooldown (12s left)" both bucket
    as "cooldown").
    """
    __tablename__ = "signal_reject_stats"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hour_bucket: Mapped[int] = mapped_column(BigInteger, index=True)  # epoch hour
    strategy: Mapped[str] = mapped_column(String(32), index=True)
    coin: Mapped[str] = mapped_column(String(32), index=True)
    reason_short: Mapped[str] = mapped_column(String(64))
    count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        Index("ix_reject_bucket", "hour_bucket", "strategy", "coin", "reason_short"),
    )


class RawWsSample(Base):
    """Sampled raw WebSocket messages, retained for feed validation/debugging.

    Only populated when CFG.raw_ws_log is True. Sampled (1 / sample_every) per
    channel to keep DB size manageable. Crucial for verifying that liquidation
    data actually arrives in the trades feed.
    """
    __tablename__ = "raw_ws_samples"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)  # trades, l2Book, unknown, error
    coin: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    has_liquidation: Mapped[bool] = mapped_column(Boolean, default=False)  # for trade samples
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # full msg
    note: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
