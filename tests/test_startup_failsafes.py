"""
Tests for the two startup/runtime fail-safes:

1. live_armed must be force-reset to False on every startup, even if it was
   True in the DB (e.g. a crash after operator armed live).

2. liquidation_fade must explicitly become 'unavailable' if the feed yields
   no liquidation-flagged trades, and stay refused until operator clears it.

These rely on a real DB so they require sqlalchemy to be installed. They use
sqlite in-memory for isolation.
"""
from __future__ import annotations
import os
import sys
import time
import asyncio
import pathlib

# Force-disable live code BEFORE importing — so SDK isn't imported.
os.environ.setdefault("LIVE_CODE_ENABLED", "false")
os.environ.setdefault("ENABLE_LIVE", "false")
os.environ.setdefault("REQUIRE_PROTECTED_EXITS", "true")
# Use sqlite in-memory for isolated tests. SQLAlchemy async needs aiosqlite
# (added to requirements.txt as a test extra).
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def _run(coro):
    """Run an async coroutine in a fresh event loop (test helper)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_live_armed_force_reset_on_startup():
    """init_db() must set live_armed=False even if a prior row had it True."""
    # Fresh import each test — but since modules cache, we need to reset the engine.
    # Work-around: use a unique sqlite memory URL per test so init_db creates fresh.
    import importlib
    from src.db import session as session_mod
    from src.db.models import BotState
    from src.db.session import init_db, session_scope, update_state

    async def _t():
        # First init: fresh DB. live_armed should be False.
        await init_db()
        async with session_scope() as s:
            st = await s.get(BotState, 1)
            assert st is not None
            assert st.live_armed is False, "fresh init should have live_armed=False"

        # Operator arms live
        await update_state(live_armed=True)
        async with session_scope() as s:
            st = await s.get(BotState, 1)
            assert st.live_armed is True

        # Simulate restart: call init_db again
        await init_db()

        # MUST be force-reset to False
        async with session_scope() as s:
            st = await s.get(BotState, 1)
            assert st.live_armed is False, (
                "STARTUP FAIL-SAFE BROKEN: live_armed not reset on restart!"
            )

    _run(_t())


def test_liq_feed_status_resets_to_unknown_on_startup():
    """init_db() must set liq_feed_status='unknown' on every startup so we
    re-validate from scratch. (The runtime listener will mark it 'validated'
    on first liq seen, or the 1-hour loop will mark it 'unavailable'.)"""
    from src.db.models import BotState
    from src.db.session import init_db, session_scope, update_state

    async def _t():
        await init_db()
        # Mark validated
        await update_state(liq_feed_status="validated")
        async with session_scope() as s:
            st = await s.get(BotState, 1)
            assert st.liq_feed_status == "validated"

        # Restart
        await init_db()
        async with session_scope() as s:
            st = await s.get(BotState, 1)
            assert st.liq_feed_status == "unknown", (
                "feed status not reset to 'unknown' on restart"
            )

    _run(_t())


def test_check_can_trade_refuses_liq_when_feed_unavailable():
    """RiskManager must refuse liquidation_fade when liq_feed_status='unavailable',
    even if liq_fade_enabled is True."""
    from src.db.session import init_db, update_state
    from src.risk.limits import RiskManager
    from src.ws.market_state import MarketState, TradeTick

    async def _t():
        await init_db()
        await update_state(
            liq_fade_enabled=True,
            liq_feed_status="unavailable",
            ws_connected=True,
            last_ws_msg_ms=int(time.time() * 1000),
        )
        market = MarketState(["BTC"])
        market.add_trade("BTC", TradeTick(
            ts_ms=int(time.time() * 1000), px=60_000, size=0.1,
            size_usd=6_000, aggressor_buy=True, is_liq=False,
        ))
        rm = RiskManager(market)
        ok, reason = await rm.check_can_trade("BTC", "liquidation_fade")
        assert ok is False
        assert "liq feed unavailable" in (reason or "").lower(), reason

    _run(_t())


def test_check_can_trade_allows_liq_when_feed_validated():
    """Sanity check: with feed validated, liq_fade is permitted (other checks aside)."""
    from src.db.session import init_db, update_state
    from src.risk.limits import RiskManager
    from src.ws.market_state import MarketState, TradeTick

    async def _t():
        await init_db()
        await update_state(
            liq_fade_enabled=True,
            liq_feed_status="validated",
            ws_connected=True,
            last_ws_msg_ms=int(time.time() * 1000),
        )
        market = MarketState(["BTC"])
        market.add_trade("BTC", TradeTick(
            ts_ms=int(time.time() * 1000), px=60_000, size=0.1,
            size_usd=6_000, aggressor_buy=True, is_liq=False,
        ))
        rm = RiskManager(market)
        ok, reason = await rm.check_can_trade("BTC", "liquidation_fade")
        assert ok is True, f"expected pass, got reject: {reason}"

    _run(_t())


def test_check_can_trade_allows_liq_when_feed_unknown():
    """During the 1-hour validation window, status='unknown' should NOT block
    the strategy (we want it to run and produce data)."""
    from src.db.session import init_db, update_state
    from src.risk.limits import RiskManager
    from src.ws.market_state import MarketState, TradeTick

    async def _t():
        await init_db()
        await update_state(
            liq_fade_enabled=True,
            liq_feed_status="unknown",
            ws_connected=True,
            last_ws_msg_ms=int(time.time() * 1000),
        )
        market = MarketState(["BTC"])
        market.add_trade("BTC", TradeTick(
            ts_ms=int(time.time() * 1000), px=60_000, size=0.1,
            size_usd=6_000, aggressor_buy=True, is_liq=False,
        ))
        rm = RiskManager(market)
        ok, reason = await rm.check_can_trade("BTC", "liquidation_fade")
        assert ok is True, f"unknown status should allow liq_fade, got reject: {reason}"

    _run(_t())
