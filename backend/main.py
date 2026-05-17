"""
main.py — FastAPI application entry point.

Startup sequence:
  1. Initialize DB tables
  2. Initialize Gemini client
  3. Auto-login to Angel One (if credentials present)
  4. Load watchlist from config
  5. Start APScheduler trading loop
  6. Mount REST routes + WebSocket
"""
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from core.config import get_settings
from core.database import init_db
from api.routes import router
from api.websocket import websocket_endpoint
from services.angel_one import angel_service
from services.gemini_engine import gemini_engine
from services.risk_manager import risk_manager
from services.trading_loop import trading_loop
from models.schemas import WatchlistItem
from utils.logger import get_logger

settings = get_settings()
logger = get_logger("main")

scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")


def load_watchlist() -> list[WatchlistItem]:
    """Load watchlist from config/watchlist.json."""
    config_path = Path("../config/watchlist.json")
    if not config_path.exists():
        logger.warning("watchlist.json not found. Using default RELIANCE watchlist.")
        return [
            WatchlistItem(symbol="RELIANCE", token="2885", exchange="NSE"),
            WatchlistItem(symbol="INFY", token="1594", exchange="NSE"),
            WatchlistItem(symbol="TCS", token="11536", exchange="NSE"),
        ]
    with open(config_path) as f:
        data = json.load(f)
    items = [WatchlistItem(**item) for item in data]
    logger.info(f"Loaded {len(items)} symbols from watchlist.json")
    return items


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── STARTUP ───────────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info(f"  AlgoTrader starting | Mode: {settings.mode_label}")
    logger.info("=" * 60)

    # 1. Init database
    await init_db()
    logger.info("✅ Database initialized")

    # 2. Init Gemini
    gemini_engine.initialize()

    # 3. Load watchlist
    watchlist = load_watchlist()
    trading_loop.set_watchlist(watchlist)

    # 4. Auto-login to Angel One
    if settings.angel_api_key and settings.angel_client_id:
        success = await angel_service.login()
        if success:
            angel_service.start_websocket(watchlist)
        else:
            logger.warning("Angel One auto-login failed. Use /auth/login endpoint manually.")
    else:
        logger.warning("Angel One credentials not configured in .env")

    # 5. Schedule trading loop
    scheduler.add_job(
        trading_loop.run_once,
        trigger="interval",
        seconds=settings.gemini_analysis_interval,
        id="trading_loop",
        name="Main Trading Loop",
        misfire_grace_time=30,
    )
    # Also schedule a daily session refresh at 8:50 AM IST
    scheduler.add_job(
        angel_service.refresh_session,
        trigger="cron",
        hour=8,
        minute=50,
        timezone="Asia/Kolkata",
        id="session_refresh",
        name="Daily Session Refresh",
    )
    # Daily risk reset at 9:10 AM IST (before market open)
    scheduler.add_job(
        risk_manager.reset_daily,
        trigger="cron",
        hour=9,
        minute=10,
        timezone="Asia/Kolkata",
        id="daily_reset",
        name="Daily PnL Reset",
    )
    scheduler.start()
    logger.info(f"✅ Scheduler started | Analysis every {settings.gemini_analysis_interval}s")
    logger.info(f"⚠️  Paper trading: {'ENABLED' if settings.paper_trading else 'DISABLED (LIVE MODE)'}")

    yield  # App running

    # ── SHUTDOWN ──────────────────────────────────────────────────────────────
    logger.info("Shutting down AlgoTrader...")
    trading_loop.stop()
    scheduler.shutdown(wait=False)
    logger.info("Shutdown complete.")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="AlgoTrader API",
    description="Automated intraday trading bot — Gemini AI × Angel One SmartAPI",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],           # Tighten this in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api/v1")


@app.websocket("/ws")
async def ws_handler(websocket: WebSocket):
    await websocket_endpoint(websocket)


@app.get("/", tags=["Health"])
async def health():
    return {
        "service": "AlgoTrader",
        "status": "running",
        "mode": settings.mode_label,
        "docs": "/docs",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )
