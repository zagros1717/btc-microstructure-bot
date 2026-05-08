"""
Entry point: starts orchestrator + FastAPI server.

Startup safety checks (FATAL — refuse to start):
  - If live_capable AND api_token is default → refuse
  - If live_code_enabled but no keys → refuse (live_executor catches this too,
    but failing fast is clearer)
  - If api_host is 0.0.0.0 (public) AND api_token is default → refuse
"""
from __future__ import annotations
import asyncio
import logging
import os
import signal
import sys

import uvicorn

from .config import CFG, validate_config
from .db import init_db, log_activity, prune_old_activity
from .orchestrator import Orchestrator
from .api.server import create_app, attach_orchestrator

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("main")


def _startup_safety_checks() -> None:
    """Fatal checks. Raises SystemExit on failure."""
    is_public = CFG.api_host in ("0.0.0.0", "::")
    is_default_token = CFG.api_token == "change-me-please"

    if is_public and is_default_token:
        log.critical(
            "SAFETY: api_host=%s is public but API_TOKEN is the default. "
            "Refusing to start. Set API_TOKEN to a strong random value.",
            CFG.api_host,
        )
        raise SystemExit(2)

    if CFG.live_code_enabled and CFG.enable_live_execution and is_default_token:
        log.critical(
            "SAFETY: live execution enabled but API_TOKEN is default. "
            "Refusing to start.",
        )
        raise SystemExit(2)


async def _periodic_prune(stop: asyncio.Event) -> None:
    try:
        while not stop.is_set():
            await prune_old_activity(keep_last_n=5000)
            try:
                await asyncio.wait_for(stop.wait(), timeout=300)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        pass


async def main_async() -> None:
    # validate_config raises on fatal errors, returns warnings as list
    try:
        warnings = validate_config()
    except RuntimeError as e:
        log.critical("CONFIG FATAL: %s", e)
        log.critical("Refusing to start. Fix env vars and try again.")
        raise SystemExit(2)
    for w in warnings:
        log.warning("CONFIG: %s", w)

    _startup_safety_checks()

    log.info("initializing database...")
    await init_db()

    # After init_db, log the fail-safe state plainly so it's obvious in logs
    log.info("STARTUP: live_armed=False (always reset on every startup)")
    log.info("STARTUP: liq_feed_status='unknown' (will validate from feed)")

    orch = Orchestrator()

    app = create_app()
    attach_orchestrator(orch)

    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    def _signal_handler() -> None:
        log.info("shutdown signal received")
        stop.set()
    for sig_name in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, sig_name), _signal_handler)
        except NotImplementedError:
            pass

    config = uvicorn.Config(
        app, host=CFG.api_host, port=CFG.api_port,
        log_level="info", access_log=False,
    )
    server = uvicorn.Server(config)

    orch_task = asyncio.create_task(orch.run())
    prune_task = asyncio.create_task(_periodic_prune(stop))
    server_task = asyncio.create_task(server.serve())

    await log_activity("info", "system", f"server listening on :{CFG.api_port}")

    done, pending = await asyncio.wait(
        [orch_task, server_task, asyncio.create_task(stop.wait())],
        return_when=asyncio.FIRST_COMPLETED,
    )
    log.info("shutting down...")

    await orch.stop()
    server.should_exit = True
    stop.set()

    for t in (orch_task, server_task, prune_task):
        try:
            await asyncio.wait_for(t, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            t.cancel()


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
