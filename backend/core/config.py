"""
core/config.py — Centralized configuration via pydantic-settings.
All values are loaded from environment variables / .env file.
"""
from functools import lru_cache
from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Safety ──────────────────────────────────────────────────────────────
    paper_trading: bool = True          # ALWAYS default to True

    # ── Angel One ───────────────────────────────────────────────────────────
    angel_api_key: str = ""
    angel_client_id: str = ""
    angel_password: str = ""
    angel_totp_secret: str = ""

    # ── Gemini ──────────────────────────────────────────────────────────────
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash-lite"

    # ── Risk Management ─────────────────────────────────────────────────────
    max_capital_per_trade: float = 10_000.0     # ₹
    max_daily_loss: float = 5_000.0             # ₹ — bot halts at this
    max_open_positions: int = 3
    trailing_stop_loss_pct: float = 0.5         # %
    gemini_analysis_interval: int = 300          # seconds

    # ── Database ────────────────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./algotrader.db"

    # ── Server ──────────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000
    secret_key: str = "change-this-in-production-min-32-chars!!"

    # ── Market hours (IST) ──────────────────────────────────────────────────
    market_open_hour: int = 9
    market_open_minute: int = 15
    market_close_hour: int = 15
    market_close_minute: int = 15

    @property
    def is_live(self) -> bool:
        return not self.paper_trading

    @property
    def mode_label(self) -> str:
        return "LIVE 🔴" if self.is_live else "PAPER 🟡"


@lru_cache
def get_settings() -> Settings:
    return Settings()
