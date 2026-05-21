from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field


class StrategyConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    min_minutes_into_hour: int = 25
    hour_end_cutoff_seconds: int = 60
    yes_price_min: float = 0.98
    yes_price_max: float = 0.99
    buffer_floor_usd: float = 550.0
    buffer_sigma_multiplier: float = 2.0
    rv_baseline_multiplier: float = 1.5
    rv_hard_ceiling_annualized: float = 0.70
    max_lines_per_hour: int = 2
    max_capital_per_line_usd: float = 1000.0
    max_capital_per_hour_usd: float = 2000.0
    orderbook_depth_safety_factor: float = 2.0
    emergency_exit_threshold: float = 0.93
    emergency_exit_rv_threshold: float = 0.70
    emergency_exit_min_minutes_remaining: int = 10
    max_day_risk_level_to_trade: int = 1


class RiskConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    daily_loss_limit_pct: float = 0.03
    weekly_loss_limit_pct: float = 0.07
    vol_spike_5min_threshold: float = 0.02
    vol_spike_5min_pause_minutes: int = 60
    vol_spike_30min_threshold: float = 0.05
    vol_spike_30min_pause_hours: int = 24
    starting_bankroll_usd: float = 50000.0


class KalshiConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    api_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    ws_url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    market_series_ticker: str = "KXBTCD"
    poll_interval_seconds: int = 5


class CoinbaseConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    ws_url: str = "wss://advanced-trade-ws.coinbase.com"
    product_id: str = "BTC-USD"
    reconnect_backoff_initial: float = 1.0
    reconnect_backoff_max: float = 60.0


class FinnhubConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    base_url: str = "https://finnhub.io/api/v1"
    refresh_time_et: str = "00:05"


class AlertsConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    discord_webhook_url: Optional[str] = None
    alert_on_fill: bool = True
    alert_on_circuit_breaker: bool = True
    alert_on_loss_limit: bool = True
    alert_on_error: bool = True


class LoggingConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    level: str = "INFO"
    log_file: str = "logs/bot.log"
    rotate_daily: bool = True


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    kalshi: KalshiConfig = Field(default_factory=KalshiConfig)
    coinbase: CoinbaseConfig = Field(default_factory=CoinbaseConfig)
    finnhub: FinnhubConfig = Field(default_factory=FinnhubConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def load_config(path: str) -> AppConfig:
    """Load and validate config from a YAML file. Returns a frozen AppConfig."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with config_path.open() as f:
        raw = yaml.safe_load(f)
    return AppConfig.model_validate(raw or {})
