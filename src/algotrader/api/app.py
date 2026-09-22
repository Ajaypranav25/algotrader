"""
HTTP + WebSocket API.

Every /api/v1 endpoint and the websocket require `Authorization: Bearer <API_AUTH_TOKEN>`
(the websocket takes `?token=` because browsers cannot set headers on it).
Only /api/health and the static dashboard page are public.
"""

import asyncio
import contextlib
import hmac
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from algotrader.brokers.angel_client import AuthError
from algotrader.config import Instrument, Settings
from algotrader.runtime import Runtime, build_runtime

logger = logging.getLogger(__name__)

DASHBOARD = Path(__file__).resolve().parents[3] / "frontend" / "index.html"
_bearer = HTTPBearer(auto_error=False)


def _token_ok(expected: str, supplied: str | None) -> bool:
    return bool(expected) and supplied is not None and hmac.compare_digest(expected.encode(), supplied.encode())


class Message(BaseModel):
    success: bool
    message: str


def create_app(settings: Settings, runtime_factory: Callable[[Settings], Runtime] = build_runtime) -> FastAPI:
    token = settings.api_auth_token.get_secret_value()
    if len(token) < 16:
        raise RuntimeError("API_AUTH_TOKEN must be set (>= 16 chars; >= 32 for live) before starting the API")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = runtime_factory(settings)
        app.state.runtime = runtime
        logger.info("AlgoTrader starting | mode=%s strategy=%s", settings.mode_label, runtime.engine.strategy.name)
        await runtime.start()
        try:
            yield
        finally:
            logger.info("AlgoTrader shutting down")
            await runtime.stop()

    app = FastAPI(title="AlgoTrader API", version="2.0.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
                       allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])

    def require_token(creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]) -> None:
        if creds is None or not _token_ok(token, creds.credentials):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing API token",
                                headers={"WWW-Authenticate": "Bearer"})

    def rt() -> Runtime:
        runtime: Runtime = app.state.runtime
        return runtime

    RT = Annotated[Runtime, Depends(rt)]
    api = APIRouter(prefix="/api/v1", dependencies=[Depends(require_token)])

    # ── status ───────────────────────────────────────────────────────────────
    def status_payload(r: Runtime) -> dict[str, Any]:
        payload = r.engine.status()
        payload["smartapi_connected"] = r.client.is_connected
        payload["feed_connected"] = r.feed.connected
        # Kept for dashboard compatibility: the strategy is constructed (and validated) at startup.
        payload["gemini_connected"] = True
        return payload

    @api.get("/status")
    async def get_status(r: RT) -> dict[str, Any]:
        return status_payload(r)

    @api.get("/positions")
    async def get_positions(r: RT) -> list[dict[str, Any]]:
        return r.engine.positions_snapshot()

    @api.get("/trades")
    async def get_trades(r: RT, limit: Annotated[int, Query(ge=1, le=500)] = 50,
                         status_filter: Annotated[str | None, Query(alias="status")] = None) -> list[dict[str, Any]]:
        rows = await r.repo.recent_trades(limit, status_filter)
        return [_trade_dict(t) for t in rows]

    @api.get("/dashboard")
    async def get_dashboard(r: RT) -> dict[str, Any]:
        trades = await r.repo.recent_trades(50)
        return {
            "status": status_payload(r),
            "positions": r.engine.positions_snapshot(),
            "recent_trades": [_trade_dict(t) for t in trades],
            "watchlist": [i.model_dump() for i in r.engine.instruments.values()],
        }

    @api.get("/gemini-logs")
    @api.get("/signals")
    async def get_signals(r: RT, limit: Annotated[int, Query(ge=1, le=200)] = 20) -> list[dict[str, Any]]:
        rows = await r.repo.recent_signals(limit)
        return [{
            "id": s.id, "symbol": s.symbol, "strategy": s.strategy, "signal": s.action,
            "target_price": s.target, "stop_loss": s.stop_loss, "confidence": s.confidence,
            "rationale": s.rationale, "decision": s.decision, "latency_ms": s.latency_ms,
            "candles_sent": s.candles, "created_at": s.created_at.isoformat(),
        } for s in rows]

    @api.get("/daily-pnl")
    async def get_daily_pnl(r: RT) -> list[dict[str, Any]]:
        rows = await r.repo.daily_history(30)
        return [{"date": d.date, "realized_pnl": d.realized_pnl, "unrealized_pnl": 0.0,
                 "total_trades": d.total_trades, "winning_trades": d.winning_trades, "is_paper": d.is_paper}
                for d in rows]

    # ── control ──────────────────────────────────────────────────────────────
    @api.post("/bot/start")
    async def start_bot(r: RT) -> Message:
        if not r.client.is_connected:
            raise HTTPException(503, "broker not connected — POST /api/v1/auth/login first")
        if r.engine.risk.halted:
            raise HTTPException(409, f"halted: {r.engine.risk.halt_reason} — POST /api/v1/bot/resume first")
        r.engine.start()
        return Message(success=True, message=f"bot started in {settings.mode_label} mode")

    @api.post("/bot/stop")
    async def stop_bot(r: RT) -> Message:
        r.engine.stop()
        return Message(success=True, message="new entries stopped; open positions remain protected")

    @api.post("/bot/kill-switch")
    async def kill_switch(r: RT) -> dict[str, Any]:
        result = await r.engine.kill_switch()
        msg = ("emergency stop executed — all positions closed" if result["success"]
               else f"emergency stop executed — FAILED to close: {', '.join(result['remaining'])}")
        return {"message": msg, **result}

    @api.post("/bot/resume")
    async def resume_bot(r: RT) -> Message:
        ok, msg = r.engine.risk.resume()
        return Message(success=ok, message=msg)

    @api.post("/auth/login")
    async def login(r: RT) -> Message:
        try:
            await r.connect_broker()
        except AuthError as exc:
            raise HTTPException(401, str(exc)) from exc
        return Message(success=True, message="broker login successful")

    # ── watchlist ────────────────────────────────────────────────────────────
    @api.get("/watchlist")
    async def get_watchlist(r: RT) -> list[Instrument]:
        return list(r.engine.instruments.values())

    @api.post("/watchlist")
    async def set_watchlist(r: RT, items: list[Instrument]) -> dict[str, Any]:
        if not items:
            raise HTTPException(422, "watchlist cannot be empty")
        if len({i.symbol for i in items}) != len(items):
            raise HTTPException(422, "duplicate symbols")
        r.set_watchlist(items)
        return {"success": True, "count": len(r.engine.instruments), "symbols": list(r.engine.instruments)}

    # ── on-demand analysis (never trades) ────────────────────────────────────
    @api.post("/analyze/{symbol}")
    async def analyze(r: RT, symbol: str) -> dict[str, Any]:
        inst = r.engine.instruments.get(symbol.upper())
        if inst is None:
            raise HTTPException(404, f"{symbol} is not in the watchlist")
        candles = await r.market_data.candles(inst)
        if not candles:
            raise HTTPException(503, "no candle data available")
        ltp = await r.market_data.current_price(inst)
        sig = await r.engine.strategy.generate(inst, candles, ltp)
        return {"symbol": inst.symbol, "candles_analyzed": len(candles), "ltp": ltp, "signal": sig.action.value,
                "target_price": sig.target, "stop_loss": sig.stop_loss, "confidence": sig.confidence,
                "rationale": sig.rationale}

    app.include_router(api)

    # ── public ───────────────────────────────────────────────────────────────
    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        r: Runtime | None = getattr(app.state, "runtime", None)
        return {"service": "AlgoTrader", "status": "running" if r else "starting", "mode": settings.mode_label}

    @app.get("/", response_model=None)
    async def dashboard() -> FileResponse | JSONResponse:
        if DASHBOARD.exists():
            return FileResponse(DASHBOARD, media_type="text/html")
        return JSONResponse({"error": "dashboard not found"}, status_code=404)

    @app.websocket("/api/websocket")
    async def websocket(ws: WebSocket, token_param: Annotated[str | None, Query(alias="token")] = None) -> None:
        if not _token_ok(token, token_param):
            await ws.close(code=4401, reason="unauthorized")
            return
        r = rt()
        await ws.accept()
        async with r.bus.subscribe() as queue:
            await ws.send_text(json.dumps({"event": "connected", "data": {
                "mode": settings.mode_label, "paper_trading": settings.paper_trading}}))
            sender = asyncio.create_task(_pump(ws, r, queue))
            try:
                while True:
                    msg = await ws.receive_text()
                    with contextlib.suppress(json.JSONDecodeError, AttributeError):
                        if json.loads(msg).get("type") == "ping":
                            await ws.send_text(json.dumps({"event": "pong"}))
            except WebSocketDisconnect:
                pass
            finally:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await sender

    async def _pump(ws: WebSocket, r: Runtime, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Forward bus events, plus periodic status and tick snapshots."""
        loop = asyncio.get_running_loop()
        next_status = next_ticks = loop.time()
        while True:
            now = loop.time()
            if now >= next_status:
                await ws.send_text(json.dumps({"event": "bot_status", "data": status_payload(r)}, default=str))
                next_status = now + 5
            if now >= next_ticks:
                ticks = {sym: px for sym in r.engine.instruments
                         if (px := r.market_data.last_price(sym)) is not None}
                if ticks:
                    await ws.send_text(json.dumps({"event": "tick_update", "data": ticks}))
                next_ticks = now + 2
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            await ws.send_text(json.dumps(event, default=str))

    return app


def _trade_dict(t: Any) -> dict[str, Any]:
    return {
        "id": t.id, "symbol": t.symbol, "signal": t.side, "quantity": t.quantity, "entry_price": t.entry_price,
        "exit_price": t.exit_price, "target_price": t.target, "stop_loss": t.stop_loss,
        "trailing_sl": t.stop_loss, "pnl": t.pnl, "fees": t.fees, "status": t.status,
        "exit_reason": t.exit_reason, "is_paper": t.mode == "paper", "order_id": t.entry_order_id,
        "strategy": t.strategy, "gemini_rationale": t.rationale,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
    }
