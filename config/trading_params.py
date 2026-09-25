"""
Roostoo Trading Bot — Typed Configuration
==========================================
Loads config.yaml + .env into validated dataclasses.
No raw dict access in the rest of the codebase.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field
from typing import List, Optional

import yaml
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Dataclasses — every config section gets its own type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExchangeConfig:
    base_url: str = "https://mock-api.roostoo.com"
    initial_portfolio_value: float = 100_000.0


@dataclass(frozen=True)
class TimeframeConfig:
    primary: str = "5m"
    confirmation: str = "15m"


@dataclass(frozen=True)
class SystemConfig:
    dry_run: bool = True
    polling_interval_seconds: int = 15
    candle_build_interval: int = 300
    max_data_age_seconds: int = 120
    time_sync_tolerance_ms: int = 30_000


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_pct: float = 0.01
    max_positions: int = 4
    max_gross_exposure_pct: float = 0.60
    min_cash_reserve_pct: float = 0.30
    max_drawdown_pct: float = 0.08
    rolling_24h_drawdown_pct: float = 0.05
    strategy_drawdown_pct: float = 0.04
    max_spread_bps: float = 50.0
    stop_alternative_pct: float = 0.005
    allow_shorting: bool = True


@dataclass(frozen=True)
class ExecutionConfig:
    maker_fee: float = 0.0005
    taker_fee: float = 0.0010
    prefer_maker: bool = True
    limit_order_timeout_seconds: int = 120
    max_slippage_pct: float = 0.002


@dataclass(frozen=True)
class ValueAreaStrategyConfig:
    enabled: bool = True
    min_confidence: float = 0.70
    vah_reclaim_candles: int = 3
    volume_profile_periods: int = 48
    tp1_scale_out_pct: float = 0.50


@dataclass(frozen=True)
class LiquiditySweepStrategyConfig:
    enabled: bool = True
    min_confidence: float = 0.75
    swing_lookback: int = 20
    displacement_min_body_pct: float = 0.60
    fvg_min_gap_pct: float = 0.001
    tp1_rr: float = 2.0


@dataclass(frozen=True)
class StrategyCConfig:
    enabled: bool = False


@dataclass(frozen=True)
class StrategiesConfig:
    value_area: ValueAreaStrategyConfig = field(default_factory=ValueAreaStrategyConfig)
    liquidity_sweep: LiquiditySweepStrategyConfig = field(
        default_factory=LiquiditySweepStrategyConfig
    )
    strategy_c: StrategyCConfig = field(default_factory=StrategyCConfig)


@dataclass(frozen=True)
class SignalConfig:
    min_confidence_to_trade: float = 0.65
    conflict_threshold: float = 0.30
    no_trade_volatility_percentile: float = 95.0


@dataclass(frozen=True)
class RateLimitConfig:
    max_requests_per_minute: int = 30
    max_order_requests_per_minute: int = 10
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 60.0
    backoff_multiplier: float = 2.0


@dataclass(frozen=True)
class DashboardConfig:
    enabled: bool = True
    ws_broadcast_interval: float = 1.0
    rest_fallback_interval: float = 2.0


@dataclass(frozen=True)
class SecretsConfig:
    """Loaded exclusively from environment variables. Never serialized."""
    api_key: str = ""
    secret_key: str = ""
    dashboard_token: str = ""
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8080
    live_trading_enabled: bool = False
    log_level: str = "INFO"


@dataclass(frozen=True)
class BotConfig:
    """Top-level configuration container."""
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    symbols: List[str] = field(default_factory=lambda: ["BTC/USD", "ETH/USD"])
    timeframes: TimeframeConfig = field(default_factory=TimeframeConfig)
    system: SystemConfig = field(default_factory=SystemConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    strategies: StrategiesConfig = field(default_factory=StrategiesConfig)
    signals: SignalConfig = field(default_factory=SignalConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    secrets: SecretsConfig = field(default_factory=SecretsConfig)


# ---------------------------------------------------------------------------
# Loader helpers
# ---------------------------------------------------------------------------

def _build_dataclass(cls, data: Optional[dict]):
    """Recursively build a dataclass from a dict, ignoring unknown keys."""
    if data is None:
        return cls()
    filtered = {}
    for f in cls.__dataclass_fields__:
        if f in data:
            ftype = cls.__dataclass_fields__[f].type
            # Handle nested dataclasses
            if isinstance(data[f], dict) and hasattr(ftype, "__dataclass_fields__"):
                filtered[f] = _build_dataclass(ftype, data[f])
            else:
                filtered[f] = data[f]
    return cls(**filtered)


def _load_secrets() -> SecretsConfig:
    """Load secrets from environment variables only."""
    return SecretsConfig(
        api_key=os.environ.get("ROOSTOO_API_KEY", ""),
        secret_key=os.environ.get("ROOSTOO_SECRET_KEY", ""),
        dashboard_token=os.environ.get("DASHBOARD_TOKEN", ""),
        dashboard_host=os.environ.get("DASHBOARD_HOST", "0.0.0.0"),
        dashboard_port=int(os.environ.get("DASHBOARD_PORT", "8080")),
        live_trading_enabled=os.environ.get("LIVE_TRADING_ENABLED", "false").lower() == "true",
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )


def load_config(
    config_path: Optional[str] = None,
    env_path: Optional[str] = None,
) -> BotConfig:
    """
    Load configuration from YAML + environment.

    Priority: env vars override YAML for secrets.
    YAML provides all tunable trading parameters.
    """
    # Load .env file if it exists
    project_root = pathlib.Path(__file__).resolve().parent.parent
    if env_path is None:
        env_file = project_root / ".env"
    else:
        env_file = pathlib.Path(env_path)
    if env_file.exists():
        load_dotenv(env_file)

    # Load YAML config
    if config_path is None:
        yaml_file = project_root / "config" / "config.yaml"
    else:
        yaml_file = pathlib.Path(config_path)

    raw: dict = {}
    if yaml_file.exists():
        with open(yaml_file, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

    # Build typed config
    exchange = _build_dataclass(ExchangeConfig, raw.get("exchange"))
    symbols = raw.get("symbols", ["BTC/USD", "ETH/USD"])
    timeframes = _build_dataclass(TimeframeConfig, raw.get("timeframes"))
    system = _build_dataclass(SystemConfig, raw.get("system"))
    risk = _build_dataclass(RiskConfig, raw.get("risk"))
    execution = _build_dataclass(ExecutionConfig, raw.get("execution"))
    signals = _build_dataclass(SignalConfig, raw.get("signals"))
    rate_limit_cfg = _build_dataclass(RateLimitConfig, raw.get("rate_limit"))
    dashboard = _build_dataclass(DashboardConfig, raw.get("dashboard"))

    # Strategies (nested)
    strat_raw = raw.get("strategies", {})
    va = _build_dataclass(ValueAreaStrategyConfig, strat_raw.get("value_area"))
    ls = _build_dataclass(LiquiditySweepStrategyConfig, strat_raw.get("liquidity_sweep"))
    sc = _build_dataclass(StrategyCConfig, strat_raw.get("strategy_c"))
    strategies = StrategiesConfig(value_area=va, liquidity_sweep=ls, strategy_c=sc)

    secrets = _load_secrets()

    return BotConfig(
        exchange=exchange,
        symbols=symbols,
        timeframes=timeframes,
        system=system,
        risk=risk,
        execution=execution,
        strategies=strategies,
        signals=signals,
        rate_limit=rate_limit_cfg,
        dashboard=dashboard,
        secrets=secrets,
    )
