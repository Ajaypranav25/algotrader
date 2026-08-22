"""
services/trading_loop.py — Main orchestration loop.

Runs on an APScheduler background job every N seconds.
Workflow per symbol:
  1. Fetch 15-min candles from Angel One
  2. Get current LTP
  3. Check trailing SL / target for open positions
  4. If no open position → send to Gemini for signal
  5. If BUY/SELL signal and risk checks pass → place order
  6. Log everything to database
"""
import asyncio
import json
from datetime import datetime, date, timezone
from typing import List, Optional, Dict

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.database import AsyncSessionLocal, Trade, GeminiLog, DailyPnL
from services.angel_one import angel_service
from services.gemini_engine import gemini_engine
from services.risk_manager import risk_manager
from models.schemas import OrderRequest, WatchlistItem, GeminiSignal
from utils.logger import get_logger

settings = get_settings()
logger = get_logger("trading_loop")


class TradingLoop:

    def __init__(self):
        self.is_running: bool = False
        self.iteration_count: int = 0
        self.last_error: Optional[str] = None
        self._watchlist: List[WatchlistItem] = []
        # In-memory broadcast queue for WebSocket push
        self.event_queue: asyncio.Queue = asyncio.Queue(maxsize=200)

    def set_watchlist(self, watchlist: List[WatchlistItem]):
        self._watchlist = watchlist
        logger.info(f"Watchlist set: {[w.symbol for w in watchlist]}")

    async def run_once(self):
        """
        Execute one full analysis cycle across all watchlist symbols.
        Called by APScheduler at each interval.
        """
        if not self.is_running:
            return

        if not risk_manager.is_market_open():
            logger.debug("Market closed — skipping cycle.")
            return

        if not angel_service.is_connected:
            logger.warning("Angel One not connected — skipping cycle.")
            return

        self.iteration_count += 1
        logger.info(f"─── Cycle #{self.iteration_count} [{datetime.now().strftime('%H:%M:%S')}] ───")

        async with AsyncSessionLocal() as db:
            for item in self._watchlist:
                try:
                    await self._process_symbol(item, db)
                except Exception as e:
                    logger.error(f"Error processing {item.symbol}: {e}", exc_info=True)
                    self.last_error = str(e)
                # Small delay between symbols to avoid API rate limits
                await asyncio.sleep(1.5)

    async def _process_symbol(self, item: WatchlistItem, db: AsyncSession):
        symbol = item.symbol
        logger.debug(f"Processing {symbol}...")

        # ── 1. Get current LTP ────────────────────────────────────────────
        ltp = angel_service.get_live_ltp(item.token)
        if not ltp:
            ltp = await angel_service.get_ltp(item.exchange, symbol, item.token)

        # ── 2. Check open position exits ──────────────────────────────────
        if symbol in risk_manager._positions and ltp:
            exit_reason = risk_manager.update_trailing_sl(symbol, ltp)
            if exit_reason:
                await self._exit_position(item, ltp, exit_reason, db)
                return      # Don't re-enter immediately after exit

        # ── 3. Skip if already in position ───────────────────────────────
        if symbol in risk_manager._positions:
            logger.debug(f"{symbol}: Position open, skipping signal fetch.")
            return

        # ── 4. Fetch candles ──────────────────────────────────────────────
        candles = await angel_service.get_candles(
            token=item.token,
            symbol=symbol,
            exchange=item.exchange,
            interval="FIFTEEN_MINUTE",
            lookback_days=1,
        )

        if not candles:
            logger.warning(f"No candle data for {symbol}.")
            return

        # ── 5. Gemini analysis ────────────────────────────────────────────
        import time
        t0 = time.time()
        signal = await gemini_engine.analyze(symbol, candles, ltp)
        latency_ms = int((time.time() - t0) * 1000)

        if not signal:
            return

        # Persist Gemini log
        await self._log_gemini(db, symbol, signal, len(candles), latency_ms)

        # Broadcast to dashboard
        await self._emit("gemini_signal", {
            "symbol": symbol,
            "signal": signal.signal,
            "target": signal.target_price,
            "stop_loss": signal.stop_loss,
            "confidence": signal.confidence,
            "rationale": signal.rationale,
            "timestamp": datetime.now().isoformat(),
        })

        # ── 6. HOLD → nothing to do ───────────────────────────────────────
        if signal.signal == "HOLD":
            return

        # ── 7. Risk checks ────────────────────────────────────────────────
        entry_price = ltp or (candles[-1].close if candles else 0)
        quantity = risk_manager.calculate_quantity(entry_price)

        allowed, reason = risk_manager.can_trade(symbol, signal, quantity, entry_price)
        if not allowed:
            logger.info(f"🚫 {symbol}: Trade blocked — {reason}")
            return

        # ── 8. Place order ────────────────────────────────────────────────
        await self._enter_position(item, signal, entry_price, quantity, db)

    async def _enter_position(
        self,
        item: WatchlistItem,
        signal: GeminiSignal,
        entry_price: float,
        quantity: int,
        db: AsyncSession,
    ):
        symbol = item.symbol
        order_req = OrderRequest(
            symbol=symbol,
            token=item.token,
            exchange=item.exchange,
            transaction_type=signal.signal,
            quantity=quantity,
            order_type="MARKET",
            product_type="INTRADAY",
        )

        order_resp = await angel_service.place_order(order_req)

        if order_resp.success:
            # Register with risk manager
            risk_manager.open_position(symbol, signal, entry_price, quantity)

            # DB record
            trade = Trade(
                symbol=symbol,
                token=item.token,
                signal=signal.signal,
                quantity=quantity,
                entry_price=entry_price,
                target_price=signal.target_price or 0.0,
                stop_loss=signal.stop_loss or 0.0,
                trailing_sl=signal.stop_loss,
                is_paper=settings.paper_trading,
                order_id=order_resp.order_id,
                gemini_rationale=signal.rationale,
                status="OPEN",
            )
            db.add(trade)
            await db.commit()

            await self._emit("trade_opened", {
                "symbol": symbol,
                "signal": signal.signal,
                "qty": quantity,
                "entry_price": entry_price,
                "target": signal.target_price,
                "stop_loss": signal.stop_loss,
                "order_id": order_resp.order_id,
                "is_paper": settings.paper_trading,
                "timestamp": datetime.now().isoformat(),
            })
            logger.info(
                f"✅ Trade entered | {signal.signal} {quantity}×{symbol} "
                f"@ ₹{entry_price} | {'PAPER' if settings.paper_trading else 'LIVE'}"
            )
        else:
            logger.error(f"Order failed for {symbol}: {order_resp.message}")

    async def _exit_position(
        self,
        item: WatchlistItem,
        exit_price: float,
        reason: str,
        db: AsyncSession,
    ):
        symbol = item.symbol
        pos = risk_manager._positions.get(symbol)
        if not pos:
            return

        # Reverse order to close position
        exit_side = "SELL" if pos.signal == "BUY" else "BUY"
        order_req = OrderRequest(
            symbol=symbol,
            token=item.token,
            exchange=item.exchange,
            transaction_type=exit_side,
            quantity=pos.quantity,
            order_type="MARKET",
            product_type="INTRADAY",
        )

        order_resp = await angel_service.place_order(order_req)
        pnl = risk_manager.close_position(symbol, exit_price)

        # Update DB record
        from sqlalchemy import update, and_
        await db.execute(
            update(Trade)
            .where(and_(Trade.symbol == symbol, Trade.status == "OPEN"))
            .values(
                exit_price=exit_price,
                pnl=pnl,
                status="CLOSED",
                closed_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()

        await self._emit("trade_closed", {
            "symbol": symbol,
            "exit_price": exit_price,
            "pnl": round(pnl, 2),
            "reason": reason,
            "timestamp": datetime.now().isoformat(),
        })
        logger.info(f"🔒 Exited {symbol} @ ₹{exit_price} | PnL: ₹{pnl:+.2f} | {reason}")

    async def _emit(self, event: str, data: dict):
        """Push event to WebSocket broadcast queue."""
        try:
            self.event_queue.put_nowait({"event": event, "data": data})
        except asyncio.QueueFull:
            pass    # Dashboard not listening; discard

    async def _log_gemini(self, db: AsyncSession, symbol: str, signal: GeminiSignal, candles: int, latency_ms: int):
        log = GeminiLog(
            symbol=symbol,
            signal=signal.signal,
            target_price=signal.target_price,
            stop_loss=signal.stop_loss,
            rationale=signal.rationale,
            raw_response=signal.model_dump_json(),
            candles_sent=candles,
            latency_ms=latency_ms,
        )
        db.add(log)
        await db.commit()

    # services/trading_loop.py
    def start(self):
        if self.is_running:
            logger.warning("Trading loop already running.")
            return
        self.is_running = True
        logger.info("▶️  Trading loop started.")

    def stop(self):
        self.is_running = False
        logger.info("⏹️  Trading loop stopped.")


# ── Singleton ─────────────────────────────────────────────────────────────────
trading_loop = TradingLoop()
