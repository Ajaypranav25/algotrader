"""
models/schemas.py — Pydantic request/response models.
"""
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field


# ── Gemini Signal ─────────────────────────────────────────────────────────────

class GeminiSignal(BaseModel):
    """Validated output structure expected from Gemini."""
    signal: str = Field(..., pattern="^(BUY|SELL|HOLD)$")
    target_price: Optional[float] = None
    stop_loss: Optional[float] = None
    rationale: str
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)


# ── Candle / OHLCV ───────────────────────────────────────────────────────────

class Candle(BaseModel):
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: int


# ── Watchlist ─────────────────────────────────────────────────────────────────

class WatchlistItem(BaseModel):
    symbol: str                        # e.g. "RELIANCE"
    token: str                         # Angel One instrument token
    exchange: str = "NSE"
    lot_size: int = 1


# ── Trade / Position ──────────────────────────────────────────────────────────

class TradeOut(BaseModel):
    id: int
    symbol: str
    signal: str
    quantity: int
    entry_price: float
    exit_price: Optional[float]
    target_price: float
    stop_loss: float
    trailing_sl: Optional[float]
    pnl: Optional[float]
    status: str
    is_paper: bool
    order_id: Optional[str]
    gemini_rationale: Optional[str]
    created_at: datetime
    closed_at: Optional[datetime]

    model_config = {"from_attributes": True}


class PositionOut(BaseModel):
    symbol: str
    token: str
    quantity: int
    avg_price: float
    ltp: float
    unrealized_pnl: float
    pnl_pct: float
    stop_loss: float
    target_price: float
    trailing_sl: float
    signal: str


# ── Dashboard State ───────────────────────────────────────────────────────────

class BotStatus(BaseModel):
    running: bool
    paper_trading: bool
    mode: str
    smartapi_connected: bool
    gemini_connected: bool
    market_open: bool
    daily_pnl: float
    daily_loss_limit: float
    halt_triggered: bool
    open_positions: int
    max_positions: int
    last_updated: datetime


class DashboardState(BaseModel):
    status: BotStatus
    positions: List[PositionOut]
    recent_trades: List[TradeOut]
    watchlist: List[WatchlistItem]


# ── Order ─────────────────────────────────────────────────────────────────────

class OrderRequest(BaseModel):
    symbol: str
    token: str
    exchange: str = "NSE"
    transaction_type: str          # BUY / SELL
    quantity: int
    price: float = 0               # 0 = market order
    order_type: str = "MARKET"
    product_type: str = "INTRADAY"


class OrderResponse(BaseModel):
    success: bool
    order_id: Optional[str]
    message: str
    is_paper: bool
