"""
Centralized configuration. Read once at startup from env vars.
All thresholds, limits, and toggles live here.

CRITICAL: Live execution defaults to OFF. Must be explicitly enabled via env
var AND through API call after manual confirmation. Never enable by accident.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import List


def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_float(key: str, default: float) -> float:
    v = os.getenv(key)
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    v = os.getenv(key)
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _env_list(key: str, default: List[str]) -> List[str]:
    v = os.getenv(key)
    if not v:
        return default
    return [s.strip().upper() for s in v.split(",") if s.strip()]


@dataclass(frozen=True)
class Config:
    # ── Connection ──────────────────────────────────────────────────────
    hl_ws_url: str = os.getenv("HL_WS_URL", "wss://api.hyperliquid.xyz/ws")
    hl_rest_url: str = os.getenv("HL_REST_URL", "https://api.hyperliquid.xyz")
    database_url: str = os.getenv("DATABASE_URL", "postgresql+asyncpg://localhost/btcbot")
    api_host: str = os.getenv("API_HOST", "0.0.0.0")
    api_port: int = _env_int("PORT", 8000)
    api_token: str = os.getenv("API_TOKEN", "change-me-please")  # for /toggle, /emergency_stop

    # ── Coin universe ───────────────────────────────────────────────────
    # liquidation_fade trades any of these
    coins: List[str] = field(default_factory=lambda: _env_list(
        "COINS", ["BTC", "ETH", "SOL", "HYPE", "AVAX", "LINK"]
    ))

    # ── Strategy toggles ────────────────────────────────────────────────
    # These are STARTUP defaults. At runtime, toggles live in BotState (DB).
    # Strategy on/off is controlled via the API which writes to DB, NOT here.
    enable_liquidation_fade: bool = _env_bool("ENABLE_LIQ_FADE", True)
    enable_liquidity_wall: bool = _env_bool("ENABLE_WALL", True)

    # ── Live execution: TWO-STAGE GATE ──────────────────────────────────
    # Stage 1 (compile-time-ish): LIVE_CODE_ENABLED=false → SDK never even imported
    # Stage 2 (load-time): ENABLE_LIVE=false → SDK not initialized this run
    # Stage 3 (runtime): live_armed=false in DB → SDK exists but won't send orders
    # All three must be true for an order to actually go to the exchange.
    live_code_enabled: bool = _env_bool("LIVE_CODE_ENABLED", False)
    enable_live_execution: bool = _env_bool("ENABLE_LIVE", False)

    # ── Observation / debug ─────────────────────────────────────────────
    raw_ws_log: bool = _env_bool("RAW_WS_LOG", False)  # log raw WS samples to disk + DB
    raw_ws_sample_every: int = _env_int("RAW_WS_SAMPLE_EVERY", 100)  # sample 1/N

    # ── Account / sizing ────────────────────────────────────────────────
    paper_starting_balance_usd: float = _env_float("PAPER_BALANCE", 1000.0)
    risk_per_trade_pct: float = _env_float("RISK_PCT", 1.5)  # % of equity per trade
    max_concurrent_positions: int = _env_int("MAX_POS", 3)  # global cap
    max_positions_per_strategy: int = _env_int("MAX_POS_STRAT", 2)
    max_positions_per_coin: int = _env_int("MAX_POS_COIN", 1)
    hardcoded_max_position_usd: float = _env_float("MAX_POS_USD", 200.0)  # absolute cap
    max_leverage: float = _env_float("MAX_LEV", 3.0)  # paper uses this for margin display

    # ── Daily / drawdown limits ─────────────────────────────────────────
    daily_loss_limit_pct: float = _env_float("DAILY_LOSS_PCT", 5.0)  # % of starting balance
    drawdown_circuit_pct: float = _env_float("DD_CIRCUIT_PCT", 20.0)  # peak-to-trough
    per_coin_cooldown_min: int = _env_int("COIN_COOLDOWN_MIN", 30)
    max_consecutive_losses: int = _env_int("MAX_CONSEC_LOSSES", 3)
    coin_stale_seconds: int = _env_int("COIN_STALE_S", 60)  # per-coin freshness

    # ── Liquidation Fade params ─────────────────────────────────────────
    # Cascade detection: sum of |size_usd| of liquidation-flagged trades in window
    liq_window_seconds: float = _env_float("LIQ_WINDOW_S", 5.0)
    liq_min_cascade_btc_usd: float = _env_float("LIQ_MIN_BTC", 500_000)
    liq_min_cascade_eth_usd: float = _env_float("LIQ_MIN_ETH", 250_000)
    liq_min_cascade_alt_usd: float = _env_float("LIQ_MIN_ALT", 150_000)
    # After cascade, wait for aggressor flip before entering
    liq_post_cascade_wait_s: float = _env_float("LIQ_WAIT_S", 3.0)
    liq_max_post_cascade_age_s: float = _env_float("LIQ_MAX_AGE_S", 12.0)
    # Risk parameters
    liq_atr_period_seconds: int = _env_int("LIQ_ATR_S", 60)
    liq_stop_atr_mult: float = _env_float("LIQ_SL_ATR", 1.5)
    liq_target_atr_mult: float = _env_float("LIQ_TP_ATR", 2.0)
    liq_time_stop_seconds: int = _env_int("LIQ_TIME_S", 600)  # 10 min

    # ── Liquidity Wall params (BTC ONLY) ────────────────────────────────
    wall_coin: str = "BTC"  # hardcoded
    wall_min_size_usd: float = _env_float("WALL_MIN_USD", 2_000_000)
    # A wall must be >= multiple_of_avg × the avg of nearby levels
    wall_size_multiple: float = _env_float("WALL_MULT", 5.0)
    # Track for this many seconds; must remain stable (not shrink >shrink_pct)
    wall_track_seconds: int = _env_int("WALL_TRACK_S", 30)
    wall_max_shrink_pct: float = _env_float("WALL_SHRINK_PCT", 25.0)
    # Trigger: price within X bps of wall + Y USD traded into it + aggressor flip
    wall_proximity_bps: float = _env_float("WALL_PROX_BPS", 5.0)
    wall_min_traded_into: float = _env_float("WALL_MIN_HIT", 300_000)
    wall_aggressor_flip_window_s: float = _env_float("WALL_FLIP_S", 10.0)
    # Risk
    wall_stop_buffer_bps: float = _env_float("WALL_SL_BPS", 15.0)
    wall_target_atr_mult: float = _env_float("WALL_TP_ATR", 1.5)
    wall_time_stop_seconds: int = _env_int("WALL_TIME_S", 300)  # 5 min

    # ── Execution slippage simulation (paper) ───────────────────────────
    slippage_bps_base: float = _env_float("SLIP_BASE_BPS", 5.0)
    slippage_bps_per_vol_pct: float = _env_float("SLIP_VOL_BPS", 15.0)
    taker_fee_bps: float = _env_float("FEE_TAKER_BPS", 4.5)

    # ── Safety guards (apply even in live mode) ─────────────────────────
    live_max_order_usd: float = _env_float("LIVE_MAX_ORDER_USD", 500.0)
    live_max_slippage_bps: float = _env_float("LIVE_MAX_SLIP_BPS", 30.0)
    heartbeat_timeout_s: int = _env_int("HEARTBEAT_TIMEOUT_S", 30)
    # Hard requirement: live mode REFUSES to send orders unless exchange-side
    # protected exits (reduce-only stop and TP) can be placed. Until that is
    # implemented, this guard MUST stay True.
    require_protected_exits: bool = _env_bool("REQUIRE_PROTECTED_EXITS", True)
    # Reconciliation: how often to compare DB live_trades vs HL state (seconds)
    reconcile_interval_s: int = _env_int("RECONCILE_INTERVAL_S", 60)

    # ── Signal observation (sampled stats so we can see why nothing fires) ──
    signal_stats_window_s: int = _env_int("SIGNAL_STATS_WIN_S", 300)  # 5 min rolling

    # ── Live wallet (only used if live_code_enabled AND enable_live_execution) ──
    hl_account_address: str = os.getenv("HL_ACCOUNT_ADDRESS", "")
    hl_secret_key: str = os.getenv("HL_SECRET_KEY", "")  # private key of agent wallet


CFG = Config()


def validate_config() -> list[str]:
    """Return list of human-readable warnings about config. Empty = all good."""
    warnings: list[str] = []
    errors: list[str] = []

    # Live-mode coherence
    if CFG.enable_live_execution and not CFG.live_code_enabled:
        errors.append(
            "ENABLE_LIVE=true but LIVE_CODE_ENABLED=false. "
            "Both must be true for live mode. Refusing."
        )
    if CFG.live_code_enabled and CFG.enable_live_execution:
        if not CFG.hl_account_address or not CFG.hl_secret_key:
            errors.append("Live enabled but HL_ACCOUNT_ADDRESS or HL_SECRET_KEY missing")
        if CFG.api_token == "change-me-please":
            errors.append("Live enabled but API_TOKEN is default — change it!")
        if CFG.require_protected_exits:
            warnings.append(
                "Live mode enabled with REQUIRE_PROTECTED_EXITS=true. "
                "Live orders will REFUSE until exchange-side stops are implemented. "
                "This is intentional and safe."
            )

    # Other warnings
    if CFG.api_token == "change-me-please":
        warnings.append("API_TOKEN is default — change it before exposing the dashboard")
    if CFG.hardcoded_max_position_usd > CFG.paper_starting_balance_usd:
        warnings.append("max position size > balance — sizing will be capped at balance")
    if CFG.risk_per_trade_pct > 5:
        warnings.append(f"risk_per_trade_pct={CFG.risk_per_trade_pct}% is aggressive")
    if CFG.max_positions_per_coin > CFG.max_positions_per_strategy:
        warnings.append("max_positions_per_coin > max_positions_per_strategy — odd config")

    # Errors are fatal
    if errors:
        raise RuntimeError("Config errors: " + " | ".join(errors))
    return warnings
