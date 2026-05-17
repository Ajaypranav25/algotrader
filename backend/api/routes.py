"""
api/routes.py — REST API endpoints for the trading bot.
"""
from datetime import datetime, date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.database import get_db, Trade, GeminiLog, DailyPnL
from models.schemas import (
    BotStatus, DashboardState, TradeOut, PositionOut, WatchlistItem, OrderResponse
)
from services.angel_one import angel_service
from services.gemini_engine import gemini_engine
from services.risk_manager import risk_manager
from services.trading_loop import trading_loop
from utils.logger import get_logger

settings = get_settings()
logger = get_logger("routes")
router = APIRouter()


# ── Status & Dashboard ────────────────────────────────────────────────────────

@router.get("/status", response_model=BotStatus, tags=["Dashboard"])
async def get_status():
    """Return current bot status for dashboard header."""
    return BotStatus(
        running=trading_loop.is_running,
        paper_trading=settings.paper_trading,
        mode=settings.mode_label,
        smartapi_connected=angel_service.is_connected,
        gemini_connected=gemini_engine.is_connected,
        market_open=risk_manager.is_market_open(),
        daily_pnl=round(risk_manager.daily_realized_pnl, 2),
        daily_loss_limit=settings.max_daily_loss,
        halt_triggered=risk_manager.halt_triggered,
        open_positions=len(risk_manager._positions),
        max_positions=settings.max_open_positions,
        last_updated=datetime.now(),
    )


@router.get("/dashboard", tags=["Dashboard"])
async def get_dashboard(db: AsyncSession = Depends(get_db)):
    """Full dashboard state: status + positions + recent trades + watchlist."""
    # Recent trades
    result = await db.execute(
        select(Trade).order_by(desc(Trade.created_at)).limit(50)
    )
    trades = [TradeOut.model_validate(t) for t in result.scalars().all()]

    return DashboardState(
        status=await get_status(),
        positions=risk_manager.get_positions_snapshot(),
        recent_trades=trades,
        watchlist=trading_loop._watchlist,
    )


# ── Bot Control ───────────────────────────────────────────────────────────────

@router.post("/bot/start", tags=["Control"])
async def start_bot():
    """Start the trading loop."""
    if not angel_service.is_connected:
        raise HTTPException(status_code=503, detail="Angel One SmartAPI not connected. Login first.")
    if not gemini_engine.is_connected:
        raise HTTPException(status_code=503, detail="Gemini not initialized.")
    if not trading_loop._watchlist:
        raise HTTPException(status_code=400, detail="Watchlist is empty.")

    trading_loop.start()
    return {"success": True, "message": f"Bot started in {settings.mode_label} mode"}


@router.post("/bot/stop", tags=["Control"])
async def stop_bot():
    """Stop the trading loop (does NOT close positions)."""
    trading_loop.stop()
    return {"success": True, "message": "Bot stopped. Open positions remain."}


@router.post("/bot/kill-switch", tags=["Control"])
async def kill_switch():
    """
    EMERGENCY KILL SWITCH:
    1. Stop the trading loop immediately
    2. Square off ALL open positions
    3. Trigger halt
    """
    logger.critical("🚨 KILL SWITCH ACTIVATED from API")

    trading_loop.stop()
    risk_manager.force_halt("Kill switch activated by user")

    result = await angel_service.cancel_all_positions()
    return {
        "success": True,
        "message": "Emergency stop executed. All positions squared off.",
        "positions_closed": result.get("closed", 0),
        "is_paper": result.get("is_paper", settings.paper_trading),
    }


@router.post("/bot/resume", tags=["Control"])
async def resume_bot():
    """Resume bot after manual halt (not after daily loss limit)."""
    risk_manager.resume()
    return {
        "success": not risk_manager.halt_triggered,
        "message": "Resumed" if not risk_manager.halt_triggered else risk_manager.halt_reason,
    }


# ── Authentication ────────────────────────────────────────────────────────────

@router.post("/auth/login", tags=["Auth"])
async def login_angel(db: AsyncSession = Depends(get_db)):
    """Trigger Angel One SmartAPI login with TOTP."""
    success = await angel_service.login()
    if success:
        # Start WebSocket data stream
        if trading_loop._watchlist:
            angel_service.start_websocket(trading_loop._watchlist)
        return {"success": True, "message": "Login successful", "mode": settings.mode_label}
    raise HTTPException(status_code=401, detail="Angel One login failed. Check credentials.")


@router.post("/auth/refresh", tags=["Auth"])
async def refresh_session():
    """Refresh the Angel One JWT token."""
    success = await angel_service.refresh_session()
    return {"success": success, "message": "Session refreshed" if success else "Refresh failed"}


# ── Watchlist ─────────────────────────────────────────────────────────────────

@router.get("/watchlist", response_model=List[WatchlistItem], tags=["Watchlist"])
async def get_watchlist():
    return trading_loop._watchlist


@router.post("/watchlist", tags=["Watchlist"])
async def set_watchlist(items: List[WatchlistItem]):
    """Replace the active watchlist."""
    trading_loop.set_watchlist(items)
    if angel_service.is_connected:
        angel_service.start_websocket(items)
    return {"success": True, "count": len(items), "symbols": [i.symbol for i in items]}


# ── Positions & Trades ────────────────────────────────────────────────────────

@router.get("/positions", response_model=List[PositionOut], tags=["Positions"])
async def get_positions():
    return risk_manager.get_positions_snapshot()


@router.get("/trades", response_model=List[TradeOut], tags=["Trades"])
async def get_trades(
    limit: int = 50,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    q = select(Trade).order_by(desc(Trade.created_at)).limit(limit)
    if status:
        q = q.where(Trade.status == status)
    result = await db.execute(q)
    return [TradeOut.model_validate(t) for t in result.scalars().all()]


@router.get("/gemini-logs", tags=["Analysis"])
async def get_gemini_logs(limit: int = 20, db: AsyncSession = Depends(get_db)):
    """Return recent Gemini analysis logs."""
    result = await db.execute(
        select(GeminiLog).order_by(desc(GeminiLog.created_at)).limit(limit)
    )
    logs = result.scalars().all()
    return [
        {
            "id": l.id,
            "symbol": l.symbol,
            "signal": l.signal,
            "target_price": l.target_price,
            "stop_loss": l.stop_loss,
            "rationale": l.rationale,
            "latency_ms": l.latency_ms,
            "candles_sent": l.candles_sent,
            "created_at": l.created_at.isoformat(),
        }
        for l in logs
    ]


@router.get("/daily-pnl", tags=["Analytics"])
async def get_daily_pnl(db: AsyncSession = Depends(get_db)):
    """Return daily PnL history for charts."""
    result = await db.execute(
        select(DailyPnL).order_by(desc(DailyPnL.date)).limit(30)
    )
    records = result.scalars().all()
    return [
        {
            "date": r.date,
            "realized_pnl": r.realized_pnl,
            "unrealized_pnl": r.unrealized_pnl,
            "total_trades": r.total_trades,
            "winning_trades": r.winning_trades,
            "is_paper": r.is_paper,
        }
        for r in records
    ]


# ── Manual Analysis (on-demand) ───────────────────────────────────────────────

@router.post("/analyze/{symbol}", tags=["Analysis"])
async def manual_analyze(symbol: str, token: str, exchange: str = "NSE"):
    """
    Trigger an on-demand Gemini analysis for a single symbol.
    Useful for testing without the full loop.
    """
    candles = await angel_service.get_candles(
        token=token, symbol=symbol, exchange=exchange,
        interval="FIFTEEN_MINUTE", lookback_days=1,
    )
    if not candles:
        raise HTTPException(status_code=404, detail="No candle data available.")

    ltp = await angel_service.get_ltp(exchange, symbol, token)
    signal = await gemini_engine.analyze(symbol, candles, ltp)

    if not signal:
        raise HTTPException(status_code=500, detail="Gemini analysis failed.")

    return {
        "symbol": symbol,
        "candles_analyzed": len(candles),
        "ltp": ltp,
        "signal": signal.signal,
        "target_price": signal.target_price,
        "stop_loss": signal.stop_loss,
        "confidence": signal.confidence,
        "rationale": signal.rationale,
    }
