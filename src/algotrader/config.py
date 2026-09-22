"""
Configuration layer.

All runtime configuration comes from environment variables (optionally a `.env`
file in the working directory). Secrets are held as `SecretStr` so they never
appear in reprs, logs or tracebacks.

Live trading is deliberately hard to turn on: it needs PAPER_TRADING=false,
LIVE_TRADING_ACKNOWLEDGED=true, broker credentials and an API auth token. Any
missing piece fails validation at startup instead of at the first order.
"""
from __future__ import annotations

import json
from datetime import time
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class TradingMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class StrategyName(StrEnum):
    EMA_CROSSOVER = "ema_crossover"
    GEMINI = "gemini"


class Instrument(BaseModel):
    """One tradable symbol from the watchlist."""

    symbol: str = Field(min_length=1, max_length=30)
    token: str = Field(pattern=r"^\d+$")
    exchange: str = "NSE"
    # Broker trading symbol. NSE cash equities are "<SYMBOL>-EQ".
    trading_symbol: str | None = None
    lot_size: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _default_trading_symbol(self) -> Instrument:
        if not self.trading_symbol:
            suffix = "-EQ" if self.exchange.upper() in {"NSE", "BSE"} else ""
            self.trading_symbol = f"{self.symbol.upper()}{suffix}"
        return self

    @property
    def broker_symbol(self) -> str:
        assert self.trading_symbol is not None
        return self.trading_symbol


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Mode / safety ────────────────────────────────────────────────────────
    paper_trading: bool = True
    live_trading_acknowledged: bool = False

    # ── Angel One ────────────────────────────────────────────────────────────
    angel_api_key: SecretStr = SecretStr("")
    angel_client_id: str = ""
    angel_password: SecretStr = SecretStr("")
    angel_totp_secret: SecretStr = SecretStr("")

    # ── Strategy ─────────────────────────────────────────────────────────────
    strategy: StrategyName = StrategyName.GEMINI
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-2.5-flash-lite"
    gemini_timeout_seconds: float = Field(default=30.0, gt=0)
    candle_interval: str = "FIFTEEN_MINUTE"
    candle_lookback_days: int = Field(default=5, ge=1, le=30)
    # Kept under its historical env name for backward compatibility.
    signal_interval_seconds: int = Field(default=300, ge=10, validation_alias="GEMINI_ANALYSIS_INTERVAL")

    # ── Risk ─────────────────────────────────────────────────────────────────
    max_capital_per_trade: float = Field(default=10_000.0, gt=0)
    max_risk_per_trade: float = Field(default=500.0, gt=0)
    max_daily_loss: float = Field(default=5_000.0, gt=0)
    max_open_positions: int = Field(default=3, ge=1)
    trailing_stop_loss_pct: float = Field(default=0.5, ge=0, lt=20)
    min_confidence: float = Field(default=0.55, ge=0, le=1)
    min_reward_risk: float = Field(default=1.5, ge=0)
    max_entry_deviation_pct: float = Field(default=1.0, gt=0)
    allow_short: bool = True
    max_orders_per_minute: int = Field(default=10, ge=1)
    max_consecutive_exit_failures: int = Field(default=3, ge=1)
    stale_price_seconds: float = Field(default=15.0, gt=0)
    risk_check_interval_seconds: float = Field(default=1.0, gt=0)

    # ── Execution costs (paper + backtest) ───────────────────────────────────
    slippage_bps: float = Field(default=2.0, ge=0)
    fee_per_order: float = Field(default=20.0, ge=0)
    order_fill_timeout_seconds: float = Field(default=15.0, gt=0)

    # ── Market session (IST) ─────────────────────────────────────────────────
    market_open: time = time(9, 15)
    entry_cutoff: time = time(14, 55)
    square_off_time: time = time(15, 10)
    market_close: time = time(15, 30)
    holidays_file: Path = Path("config/nse_holidays.json")

    # ── Storage / files ──────────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./data/algotrader.db"
    watchlist_file: Path = Path("config/watchlist.json")
    log_dir: Path = Path("logs")
    log_level: str = "INFO"
    log_format: str = Field(default="console", pattern="^(console|json)$")

    # ── API server ───────────────────────────────────────────────────────────
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    api_auth_token: SecretStr = SecretStr("")
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:8000", "http://127.0.0.1:8000", "http://localhost:3000"]
    )

    # ── Validators ───────────────────────────────────────────────────────────
    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log level {v!r}")
        return v

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        if not (self.market_open < self.entry_cutoff < self.square_off_time <= self.market_close):
            raise ValueError(
                "session times must satisfy market_open < entry_cutoff < square_off_time <= market_close"
            )
        if self.max_risk_per_trade > self.max_capital_per_trade:
            raise ValueError("max_risk_per_trade cannot exceed max_capital_per_trade")

        if not self.paper_trading:
            missing = [
                name
                for name, value in (
                    ("ANGEL_API_KEY", self.angel_api_key.get_secret_value()),
                    ("ANGEL_CLIENT_ID", self.angel_client_id),
                    ("ANGEL_PASSWORD", self.angel_password.get_secret_value()),
                    ("ANGEL_TOTP_SECRET", self.angel_totp_secret.get_secret_value()),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"live trading requires {', '.join(missing)}")
            if not self.live_trading_acknowledged:
                raise ValueError(
                    "PAPER_TRADING=false also requires LIVE_TRADING_ACKNOWLEDGED=true — "
                    "an explicit second switch so live mode is never enabled by accident"
                )
            if len(self.api_auth_token.get_secret_value()) < 32:
                raise ValueError("live trading requires API_AUTH_TOKEN of at least 32 characters")

        if self.strategy is StrategyName.GEMINI and not self.gemini_api_key.get_secret_value():
            # Not fatal for backtests with other strategies; the factory raises when actually used.
            pass
        return self

    # ── Convenience ──────────────────────────────────────────────────────────
    @property
    def mode(self) -> TradingMode:
        return TradingMode.PAPER if self.paper_trading else TradingMode.LIVE

    @property
    def mode_label(self) -> str:
        return "LIVE 🔴" if self.mode is TradingMode.LIVE else "PAPER 🟡"

    @property
    def has_broker_credentials(self) -> bool:
        return bool(self.angel_api_key.get_secret_value() and self.angel_client_id)

    def secret_values(self) -> list[str]:
        """Every configured secret, for log redaction."""
        secrets = [
            self.angel_api_key,
            self.angel_password,
            self.angel_totp_secret,
            self.gemini_api_key,
            self.api_auth_token,
        ]
        return [s.get_secret_value() for s in secrets if len(s.get_secret_value()) >= 4]


def load_watchlist(path: Path) -> list[Instrument]:
    """Load and validate the watchlist. Raises on a malformed file rather than trading a guess."""
    if not path.exists():
        raise FileNotFoundError(f"watchlist not found: {path.resolve()}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path} must contain a non-empty JSON list")
    items = [Instrument.model_validate(item) for item in raw]
    symbols = [i.symbol for i in items]
    if len(set(symbols)) != len(symbols):
        raise ValueError(f"duplicate symbols in {path}")
    return items


@lru_cache
def get_settings() -> Settings:
    return Settings()
