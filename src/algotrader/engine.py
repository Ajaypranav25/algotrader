"""
Trading engine — orchestrates data → strategy → risk → execution.

Two independent loops:

* **signal loop** (every `signal_interval_seconds`, only while *running*):
  fetch candles, ask the strategy, run the risk gate, place entries.
* **protection loop** (every `risk_check_interval_seconds`, ALWAYS while the
  process is up — even when the bot is "stopped" or halted): trail stops,
  exit on stop/target, enforce the daily loss limit and the end-of-day
  square-off. Stopping the bot stops new entries; it never leaves open
  positions unprotected.

Order safety invariants
* A position is only created from a confirmed fill, at the fill price.
* A position is only removed after a confirmed exit fill; failed exits are
  retried with backoff and escalate to a halt.
* An UNKNOWN order outcome halts new entries immediately.
* Entry and exit for a symbol are serialised by a per-symbol lock.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from algotrader.brokers.base import Broker
from algotrader.config import Instrument, Settings
from algotrader.domain import Candle, ExitReason, OrderRequest, OrderStatus, Side, Signal
from algotrader.events import EventBus
from algotrader.market_calendar import Clock, MarketCalendar
from algotrader.persistence import Repository
from algotrader.risk import EntryDecision, RiskManager
from algotrader.strategies.base import Strategy

logger = logging.getLogger(__name__)


class MarketData(Protocol):
    async def candles(self, instrument: Instrument) -> list[Candle]: ...

    async def current_price(self, instrument: Instrument) -> float | None:
        """A fresh price (live tick, or a REST quote if the tick is stale). None if unavailable."""
        ...

    def last_price(self, symbol: str) -> float | None:
        """Most recent known price without I/O (used by the paper broker)."""
        ...


@dataclass(slots=True)
class EngineStats:
    cycles: int = 0
    last_cycle_at: datetime | None = None
    last_error: str | None = None


BrokerPositionsFn = Callable[[], Awaitable[list[dict[str, Any]]]]


class TradingEngine:
    def __init__(
        self,
        settings: Settings,
        strategy: Strategy,
        risk: RiskManager,
        broker: Broker,
        market_data: MarketData,
        repo: Repository,
        bus: EventBus,
        calendar: MarketCalendar,
        clock: Clock,
        instruments: Sequence[Instrument],
        broker_positions: BrokerPositionsFn | None = None,
    ) -> None:
        self.settings = settings
        self.strategy = strategy
        self.risk = risk
        self.broker = broker
        self.md = market_data
        self.repo = repo
        self.bus = bus
        self.calendar = calendar
        self.clock = clock
        self.instruments: dict[str, Instrument] = {i.symbol: i for i in instruments}
        self._broker_positions = broker_positions
        self.running = False
        self.stats = EngineStats()
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._exit_retry_at: dict[str, float] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._strategy_names: dict[str, str] = {}

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def launch(self) -> None:
        """Start background loops. The protection loop starts immediately."""
        self.risk.roll_day(self.clock.now().date())
        self._tasks = [
            asyncio.create_task(self._loop(self.protect_once, self.settings.risk_check_interval_seconds,
                                           "protection"), name="protection-loop"),
            asyncio.create_task(self._loop(self._signal_cycle_if_running, self.settings.signal_interval_seconds,
                                           "signal"), name="signal-loop"),
        ]

    async def shutdown(self) -> None:
        self.running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks.clear()
        if self.risk.positions:
            logger.warning("shutting down with %d open positions — they will be restored on next start",
                           len(self.risk.positions))

    def start(self) -> None:
        self.running = True
        logger.info("signal generation started (%s)", self.settings.mode_label)
        self._publish_status()

    def stop(self) -> None:
        self.running = False
        logger.info("signal generation stopped — open positions remain protected")
        self._publish_status()

    def set_instruments(self, instruments: Iterable[Instrument]) -> None:
        new = {i.symbol: i for i in instruments}
        dropped = set(self.risk.positions) - set(new)
        for sym in dropped:
            # Keep tracking instruments that still have positions.
            new[sym] = self.instruments[sym]
        self.instruments = new

    async def _loop(self, fn: Callable[[], Awaitable[None]], interval: float, name: str) -> None:
        while True:
            started = time.monotonic()
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a loop must never die
                self.stats.last_error = f"{name}: {exc}"
                logger.exception("%s loop iteration failed", name)
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))

    # ── signal loop ──────────────────────────────────────────────────────────
    async def _signal_cycle_if_running(self) -> None:
        if self.running:
            await self.signal_cycle()

    async def signal_cycle(self) -> None:
        now = self.clock.now()
        if not self.calendar.entries_allowed(now):
            return
        if self.risk.halted:
            logger.debug("halted (%s) — skipping signal cycle", self.risk.halt_reason)
            return
        self.stats.cycles += 1
        self.stats.last_cycle_at = now
        for inst in list(self.instruments.values()):
            if not self.running or self.risk.halted:
                break
            if inst.symbol in self.risk.positions or inst.symbol in self.risk.pending:
                continue
            try:
                await self._evaluate(inst)
            except Exception as exc:
                self.stats.last_error = f"{inst.symbol}: {exc}"
                logger.exception("signal evaluation failed for %s", inst.symbol)

    async def _evaluate(self, inst: Instrument) -> None:
        candles = await self.md.candles(inst)
        if len(candles) < self.strategy.warmup:
            logger.debug("%s: %d candles < warmup %d", inst.symbol, len(candles), self.strategy.warmup)
            return
        ltp = await self.md.current_price(inst)
        signal = await self.strategy.generate(inst, candles, ltp)
        latency = getattr(self.strategy, "last_latency_ms", None)

        if signal.side is None:
            await self._record_signal(signal, "HOLD", latency, len(candles))
            return
        if ltp is None:
            await self._record_signal(signal, "rejected: no fresh price", latency, len(candles))
            return

        decision = self.risk.evaluate_entry(signal, ltp, self.clock.now())
        await self._record_signal(signal, "approved" if decision.approved else f"rejected: {decision.reason}",
                                  latency, len(candles))
        if decision.approved:
            await self._enter(inst, signal, decision)
        else:
            logger.info("%s %s blocked: %s", inst.symbol, signal.action.value, decision.reason)

    async def _record_signal(self, sig: Signal, decision: str, latency: int | None, candles: int) -> None:
        self.bus.publish("gemini_signal", {
            "symbol": sig.symbol, "signal": sig.action.value, "target": sig.target, "stop_loss": sig.stop_loss,
            "confidence": sig.confidence, "rationale": sig.rationale, "strategy": sig.strategy,
            "decision": decision, "timestamp": self.clock.now().isoformat(),
        })
        try:
            await self.repo.record_signal(sig, decision, latency, candles)
        except Exception:
            logger.exception("failed to persist signal for %s", sig.symbol)

    # ── entries ──────────────────────────────────────────────────────────────
    async def _enter(self, inst: Instrument, signal: Signal, decision: EntryDecision) -> None:
        assert decision.side is not None
        async with self._locks[inst.symbol]:
            if inst.symbol in self.risk.positions:
                return
            self.risk.pending.add(inst.symbol)
            try:
                req = OrderRequest(inst.symbol, inst.broker_symbol, inst.token, inst.exchange,
                                   decision.side, decision.quantity, tag=f"entry-{inst.symbol}")
                result = await self.broker.place_order(req)
                if result.status is OrderStatus.UNKNOWN:
                    self.risk.halt(f"entry order for {inst.symbol} unconfirmed ({result.message}) — "
                                   "check the broker terminal and reconcile before resuming")
                    self.bus.publish("alert", {"level": "critical", "message": self.risk.halt_reason})
                    return
                if not result.filled:
                    logger.warning("entry rejected for %s: %s", inst.symbol, result.message)
                    return

                pos = self.risk.open_position(
                    inst.symbol, decision.side, result.filled_quantity, result.average_price,
                    decision.stop_loss, decision.target, self.clock.now(), fees=result.fees,
                    order_id=result.order_id)
                self._strategy_names[inst.symbol] = signal.strategy
                try:
                    pos.trade_id = await self.repo.record_entry(pos, self.clock.now().date(),
                                                                signal.strategy, signal.rationale)
                except Exception:
                    logger.exception("failed to persist entry for %s (position is still tracked)", inst.symbol)
            finally:
                self.risk.pending.discard(inst.symbol)

        self.bus.publish("trade_opened", {
            "symbol": inst.symbol, "signal": decision.side.value, "qty": result.filled_quantity,
            "entry_price": result.average_price, "target": decision.target, "stop_loss": decision.stop_loss,
            "order_id": result.order_id, "is_paper": self.settings.paper_trading,
            "timestamp": self.clock.now().isoformat(),
        })

    # ── protection loop ──────────────────────────────────────────────────────
    async def protect_once(self) -> None:
        now = self.clock.now()
        if self.risk.roll_day(now.date()):
            self._publish_status()

        if not self.risk.positions:
            return

        if self.calendar.must_square_off(now):
            await self.flatten_all(ExitReason.SQUARE_OFF)
            return

        for symbol in list(self.risk.positions):
            inst = self.instruments.get(symbol)
            if inst is None:
                continue
            price = await self.md.current_price(inst)
            if price is None:
                logger.warning("no fresh price for open position %s", symbol)
                continue
            reason = self.risk.on_price(symbol, price)
            if reason is not None:
                await self.exit_position(symbol, reason)

        if self.risk.positions and self.risk.loss_limit_breached():
            self.risk.halt(f"daily loss limit ₹{self.settings.max_daily_loss:,.0f} reached (incl. unrealized)",
                           daily_loss=True)
            await self.flatten_all(ExitReason.DAILY_LOSS_LIMIT)

    async def flatten_all(self, reason: ExitReason) -> int:
        closed = 0
        for symbol in list(self.risk.positions):
            if await self.exit_position(symbol, reason):
                closed += 1
        return closed

    async def exit_position(self, symbol: str, reason: ExitReason) -> bool:
        """Exit a tracked position. Returns True only on a confirmed full exit."""
        retry_at = self._exit_retry_at.get(symbol, 0.0)
        if time.monotonic() < retry_at and reason not in (ExitReason.KILL_SWITCH,):
            return False
        async with self._locks[symbol]:
            pos = self.risk.positions.get(symbol)
            inst = self.instruments.get(symbol)
            if pos is None or inst is None:
                return False
            self.risk.pending.add(symbol)
            try:
                req = OrderRequest(symbol, inst.broker_symbol, inst.token, inst.exchange, pos.side.opposite,
                                   pos.quantity, tag=f"exit-{symbol}")
                result = await self.broker.place_order(req)
                if not result.filled:
                    failures = self.risk.record_exit_failure(symbol)
                    self._exit_retry_at[symbol] = time.monotonic() + min(30.0, 2.0 ** failures)
                    if result.status is OrderStatus.UNKNOWN:
                        self.risk.halt(f"exit order for {symbol} unconfirmed — verify at broker")
                    msg = f"EXIT FAILED for {symbol} ({reason.value}): {result.message} [attempt {failures}]"
                    logger.error(msg)
                    self.bus.publish("alert", {"level": "error", "message": msg})
                    return False

                self._exit_retry_at.pop(symbol, None)
                trade_id, final_stop = pos.trade_id, pos.stop_loss
                pnl = self.risk.close_position(symbol, result.average_price, result.fees,
                                               quantity=result.filled_quantity)
                fully_closed = symbol not in self.risk.positions
            finally:
                self.risk.pending.discard(symbol)

        if fully_closed and trade_id is not None:
            try:
                await self.repo.record_exit(trade_id, result.average_price, pnl, result.fees, reason,
                                            result.order_id, final_stop)
                day = self.risk.trading_day or self.clock.now().date()
                await self.repo.upsert_daily(day, self.risk.realized_pnl, self.risk.trades_today,
                                             self.risk.wins_today)
            except Exception:
                logger.exception("failed to persist exit for %s", symbol)
        self.bus.publish("trade_closed", {
            "symbol": symbol, "exit_price": result.average_price, "pnl": round(pnl, 2), "reason": reason.value,
            "partial": not fully_closed, "timestamp": self.clock.now().isoformat(),
        })
        return fully_closed

    # ── operator actions ─────────────────────────────────────────────────────
    async def kill_switch(self) -> dict[str, Any]:
        logger.critical("KILL SWITCH activated")
        self.running = False
        self.risk.halt("kill switch activated by operator")
        closed = await self.flatten_all(ExitReason.KILL_SWITCH)
        untracked = 0
        if self._broker_positions is not None:
            untracked = await self._flatten_untracked()
        remaining = list(self.risk.positions)
        self._publish_status()
        return {"positions_closed": closed + untracked, "remaining": remaining,
                "success": not remaining, "is_paper": self.settings.paper_trading}

    async def _flatten_untracked(self) -> int:
        """Square off broker-side intraday positions the engine does not know about (live only)."""
        assert self._broker_positions is not None
        try:
            rows = await self._broker_positions()
        except Exception:
            logger.exception("could not fetch broker positions during kill switch")
            return 0
        closed = 0
        for row in rows:
            try:
                qty = int(float(row.get("netqty", 0)))
            except (TypeError, ValueError):
                continue
            if qty == 0 or str(row.get("producttype", "")).upper() not in {"INTRADAY", "MIS"}:
                continue
            symbol = str(row.get("symbolname") or row.get("tradingsymbol"))
            if symbol in self.risk.positions:
                continue
            req = OrderRequest(symbol, str(row["tradingsymbol"]), str(row["symboltoken"]),
                               str(row.get("exchange", "NSE")), Side.SELL if qty > 0 else Side.BUY, abs(qty),
                               tag="kill-switch")
            result = await self.broker.place_order(req)
            if result.filled:
                closed += 1
            else:
                logger.error("kill switch could not close broker position %s: %s", symbol, result.message)
        return closed

    # ── recovery ─────────────────────────────────────────────────────────────
    async def reconcile(self) -> None:
        """Restore today's open trades after a restart and cross-check them with the broker."""
        today = self.clock.now().date().isoformat()
        rows = await self.repo.open_trades()
        stale = [r.id for r in rows if r.trading_day != today]
        if stale:
            logger.warning("marking %d open trades from previous days as ORPHANED", len(stale))
            await self.repo.mark_orphaned(stale)
        for row in (r for r in rows if r.trading_day == today):
            if row.symbol in self.risk.positions:
                continue
            if row.symbol not in self.instruments:
                self.risk.halt(f"open trade for {row.symbol} is not in the watchlist — reconcile manually")
                continue
            pos = self.risk.open_position(row.symbol, Side(row.side), row.quantity, row.entry_price,
                                          row.initial_stop, row.target, row.created_at, trade_id=row.id,
                                          order_id=row.entry_order_id)
            pos.entry_fees = row.fees
            # Restore the ratcheted stop without loosening it.
            pos.stop_loss = row.stop_loss
            self._strategy_names[row.symbol] = row.strategy
            logger.warning("restored open position from database", extra={"ctx": {
                "symbol": row.symbol, "side": row.side, "qty": row.quantity}})

        if self._broker_positions is None:
            return
        try:
            broker_rows = await self._broker_positions()
        except Exception as exc:
            self.risk.halt(f"cannot verify broker positions on startup: {exc}")
            return
        net_by_symbol: dict[str, int] = {}
        for r in broker_rows:
            sym = str(r.get("symbolname") or r.get("tradingsymbol", "")).upper()
            try:
                net_by_symbol[sym] = net_by_symbol.get(sym, 0) + int(float(r.get("netqty", 0)))
            except (TypeError, ValueError):
                continue
        for sym, pos in self.risk.positions.items():
            broker_qty = net_by_symbol.get(sym.upper(), 0)
            expected = pos.quantity * pos.side.sign
            if broker_qty != expected:
                self.risk.halt(f"position mismatch for {sym}: engine {expected}, broker {broker_qty}")

    # ── status ───────────────────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        now = self.clock.now()
        return {
            "running": self.running,
            "paper_trading": self.settings.paper_trading,
            "mode": self.settings.mode_label,
            "strategy": self.strategy.name,
            "market_open": self.calendar.is_open(now),
            "entries_allowed": self.calendar.entries_allowed(now),
            "daily_pnl": round(self.risk.realized_pnl, 2),
            "unrealized_pnl": round(self.risk.unrealized_pnl, 2),
            "daily_loss_limit": self.settings.max_daily_loss,
            "halt_triggered": self.risk.halted,
            "halt_reason": self.risk.halt_reason,
            "open_positions": len(self.risk.positions),
            "max_positions": self.settings.max_open_positions,
            "cycles": self.stats.cycles,
            "last_error": self.stats.last_error,
            "last_updated": now.isoformat(),
        }

    def positions_snapshot(self) -> list[dict[str, Any]]:
        out = []
        for pos in self.risk.positions.values():
            notional = pos.entry_price * pos.quantity
            upnl = pos.unrealized_pnl()
            out.append({
                "symbol": pos.symbol,
                "token": self.instruments[pos.symbol].token if pos.symbol in self.instruments else "",
                "signal": pos.side.value, "quantity": pos.quantity, "avg_price": pos.entry_price,
                "ltp": pos.last_price, "unrealized_pnl": round(upnl, 2),
                "pnl_pct": round(upnl / notional * 100, 2) if notional else 0.0,
                "stop_loss": pos.stop_loss, "trailing_sl": pos.stop_loss, "target_price": pos.target,
                "opened_at": pos.opened_at.isoformat(),
            })
        return out

    def _publish_status(self) -> None:
        self.bus.publish("bot_status", self.status())
