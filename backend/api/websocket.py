"""
api/websocket.py — WebSocket endpoint pushing live events to the frontend.

Events pushed:
  - gemini_signal: New Gemini analysis result
  - trade_opened: New position entered
  - trade_closed: Position exited
  - bot_status: Periodic status ping (every 5s)
  - tick_update: Live price updates for watchlist
"""
import asyncio
import json
from datetime import datetime
from typing import Set

from fastapi import WebSocket, WebSocketDisconnect
from services.trading_loop import trading_loop
from services.risk_manager import risk_manager
from services.angel_one import angel_service
from core.config import get_settings
from utils.logger import get_logger

settings = get_settings()
logger = get_logger("websocket")


class ConnectionManager:
    """Manages all active WebSocket connections from the frontend."""

    def __init__(self):
        self.active: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)
        logger.info(f"WebSocket client connected. Total: {len(self.active)}")

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)
        logger.info(f"WebSocket client disconnected. Total: {len(self.active)}")

    async def broadcast(self, message: dict):
        """Send to all connected clients."""
        payload = json.dumps(message)
        dead = set()
        for ws in list(self.active):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self.active.discard(ws)


manager = ConnectionManager()


async def websocket_endpoint(ws: WebSocket):
    """
    Main WebSocket handler.
    - Drains the trading_loop.event_queue and broadcasts events.
    - Sends periodic status pings.
    """
    await manager.connect(ws)

    # Send initial state immediately on connect
    await ws.send_text(json.dumps({
        "event": "connected",
        "data": {
            "mode": settings.mode_label,
            "paper_trading": settings.paper_trading,
            "timestamp": datetime.now().isoformat(),
        }
    }))

    ping_task = asyncio.create_task(_status_ping_loop(ws))
    tick_task = asyncio.create_task(_tick_broadcast_loop(ws))

    try:
        while True:
            # Drain event queue
            try:
                event = trading_loop.event_queue.get_nowait()
                await manager.broadcast(event)
            except asyncio.QueueEmpty:
                pass

            # Listen for client messages (e.g., ping/pong or commands)
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=0.1)
                data = json.loads(msg)
                if data.get("type") == "ping":
                    await ws.send_text(json.dumps({"event": "pong", "ts": datetime.now().isoformat()}))
            except asyncio.TimeoutError:
                pass
            except WebSocketDisconnect:
                break

            await asyncio.sleep(0.2)

    except WebSocketDisconnect:
        pass
    finally:
        ping_task.cancel()
        tick_task.cancel()
        manager.disconnect(ws)


async def _status_ping_loop(ws: WebSocket):
    """Push bot status every 5 seconds."""
    while True:
        try:
            status_payload = {
                "event": "bot_status",
                "data": {
                    "running": trading_loop.is_running,
                    "paper_trading": settings.paper_trading,
                    "smartapi_connected": angel_service.is_connected,
                    "daily_pnl": round(risk_manager.daily_realized_pnl, 2),
                    "halt_triggered": risk_manager.halt_triggered,
                    "open_positions": len(risk_manager._positions),
                    "market_open": risk_manager.is_market_open(),
                    "timestamp": datetime.now().isoformat(),
                }
            }
            await ws.send_text(json.dumps(status_payload))
        except Exception:
            break
        await asyncio.sleep(5)


async def _tick_broadcast_loop(ws: WebSocket):
    """Push live tick prices for watchlist every 2 seconds."""
    while True:
        try:
            ticks = {}
            for item in trading_loop._watchlist:
                ltp = angel_service.get_live_ltp(item.token)
                if ltp:
                    ticks[item.symbol] = ltp

            if ticks:
                await ws.send_text(json.dumps({
                    "event": "tick_update",
                    "data": ticks,
                    "timestamp": datetime.now().isoformat(),
                }))
        except Exception:
            break
        await asyncio.sleep(2)
