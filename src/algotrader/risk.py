"""
Risk management — pure, synchronous and I/O-free so it is exhaustively testable
and shared unchanged by the live engine and the backtester.

Responsibilities
  * pre-trade gate: every entry must pass every check, and sizing comes from here
  * position book: the single source of truth for open positions and their stops
  * daily loss limit on realized + unrealized PnL, with a latching halt
  * order-rate throttle
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime

from algotrader.config import Settings
from algotrader.domain import ExitReason, Position, Side, Signal
from algotrader.market_calendar import MarketCalendar
from algotrader.resilience import SlidingWindowLimiter

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_capital_per_trade: float
    max_risk_per_trade: float
    max_daily_loss: float
    max_open_positions: int
    trailing_stop_pct: float  # percent, e.g. 0.5
    min_confidence: float
    min_reward_risk: float
    max_entry_deviation_pct: float
    allow_short: bool
    max_orders_per_minute: int
    max_consecutive_exit_failures: int

    @classmethod
    def from_settings(cls, s: Settings) -> RiskLimits:
        return cls(
            max_capital_per_trade=s.max_capital_per_trade,
            max_risk_per_trade=s.max_risk_per_trade,
            max_daily_loss=s.max_daily_loss,
            max_open_positions=s.max_open_positions,
            trailing_stop_pct=s.trailing_stop_loss_pct,
            min_confidence=s.min_confidence,
            min_reward_risk=s.min_reward_risk,
            max_entry_deviation_pct=s.max_entry_deviation_pct,
            allow_short=s.allow_short,
            max_orders_per_minute=s.max_orders_per_minute,
            max_consecutive_exit_failures=s.max_consecutive_exit_failures,
        )


@dataclass(frozen=True, slots=True)
class EntryDecision:
    approved: bool
    reason: str
    quantity: int = 0
    side: Side | None = None
    stop_loss: float = 0.0
    target: float | None = None

    @classmethod
    def reject(cls, reason: str) -> EntryDecision:
        return cls(approved=False, reason=reason)


class RiskManager:
    def __init__(self, limits: RiskLimits, calendar: MarketCalendar,
                 order_limiter: SlidingWindowLimiter | None = None) -> None:
        self.limits = limits
        self.calendar = calendar
        self._order_limiter = order_limiter or SlidingWindowLimiter(limits.max_orders_per_minute, 60.0)
        self.positions: dict[str, Position] = {}
        self.pending: set[str] = set()  # symbols with an in-flight entry/exit order
        self.realized_pnl: float = 0.0
        self.trades_today: int = 0
        self.wins_today: int = 0
        self.trading_day: date | None = None
        self.halted: bool = False
        self.halt_reason: str = ""
        self._halt_is_daily_loss: bool = False

    # ── Day lifecycle ────────────────────────────────────────────────────────
    def roll_day(self, today: date) -> bool:
        """Reset daily counters when the date changes. Returns True if a reset happened."""
        if self.trading_day == today:
            return False
        if self.positions:
            # Intraday positions should never survive overnight; the engine squares
            # off before close. Keep them tracked so they are still protected.
            logger.error("day rolled with %d open positions still tracked", len(self.positions))
        self.trading_day = today
        self.realized_pnl = 0.0
        self.trades_today = 0
        self.wins_today = 0
        if self.halted and self._halt_is_daily_loss:
            self.halted, self.halt_reason, self._halt_is_daily_loss = False, "", False
            logger.info("daily-loss halt cleared for new trading day %s", today)
        return True

    # ── Halt control ─────────────────────────────────────────────────────────
    def halt(self, reason: str, *, daily_loss: bool = False) -> None:
        if not self.halted:
            logger.critical("TRADING HALTED: %s", reason)
        self.halted = True
        self.halt_reason = reason
        self._halt_is_daily_loss = self._halt_is_daily_loss or daily_loss

    def resume(self) -> tuple[bool, str]:
        if not self.halted:
            return True, "not halted"
        if self._halt_is_daily_loss:
            return False, "daily loss limit reached — resets next trading day"
        self.halted, self.halt_reason = False, ""
        logger.warning("trading resumed by operator")
        return True, "resumed"

    # ── Aggregates ───────────────────────────────────────────────────────────
    @property
    def unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl() for p in self.positions.values())

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl

    def loss_limit_breached(self) -> bool:
        return self.total_pnl <= -self.limits.max_daily_loss

    # ── Pre-trade gate ───────────────────────────────────────────────────────
    def evaluate_entry(self, signal: Signal, price: float, now: datetime) -> EntryDecision:
        """
        Decide whether `signal` may be traded at `price` and how big.
        Pure except for consuming an order-rate token when approved.
        """
        side = signal.side
        if side is None:
            return EntryDecision.reject("signal is HOLD")
        if self.halted:
            return EntryDecision.reject(f"halted: {self.halt_reason}")
        if not self.calendar.entries_allowed(now):
            return EntryDecision.reject("outside entry window")
        if side is Side.SELL and not self.limits.allow_short:
            return EntryDecision.reject("short selling disabled")
        if not math.isfinite(price) or price <= 0:
            return EntryDecision.reject(f"invalid price {price}")
        if signal.symbol in self.positions:
            return EntryDecision.reject("position already open")
        if signal.symbol in self.pending:
            return EntryDecision.reject("order already in flight")
        if len(self.positions) + len(self.pending) >= self.limits.max_open_positions:
            return EntryDecision.reject(f"max open positions ({self.limits.max_open_positions}) reached")
        if self.loss_limit_breached():
            self.halt(f"daily loss limit ₹{self.limits.max_daily_loss:,.0f} reached", daily_loss=True)
            return EntryDecision.reject(self.halt_reason)
        if signal.confidence is not None and signal.confidence < self.limits.min_confidence:
            return EntryDecision.reject(
                f"confidence {signal.confidence:.2f} below minimum {self.limits.min_confidence:.2f}")

        stop, target = signal.stop_loss, signal.target
        if stop is None or not math.isfinite(stop) or stop <= 0:
            return EntryDecision.reject("signal has no valid stop loss")
        if side is Side.BUY and stop >= price:
            return EntryDecision.reject(f"BUY stop {stop} is not below price {price}")
        if side is Side.SELL and stop <= price:
            return EntryDecision.reject(f"SELL stop {stop} is not above price {price}")
        if target is not None:
            if side is Side.BUY and target <= price:
                return EntryDecision.reject(f"BUY target {target} is not above price {price}")
            if side is Side.SELL and target >= price:
                return EntryDecision.reject(f"SELL target {target} is not below price {price}")

        risk_per_share = abs(price - stop)
        if target is not None and self.limits.min_reward_risk > 0:
            reward_risk = abs(target - price) / risk_per_share
            if reward_risk < self.limits.min_reward_risk:
                return EntryDecision.reject(
                    f"reward:risk {reward_risk:.2f} below minimum {self.limits.min_reward_risk:.2f}")

        if signal.reference_price:
            deviation = abs(price - signal.reference_price) / signal.reference_price * 100
            if deviation > self.limits.max_entry_deviation_pct:
                return EntryDecision.reject(
                    f"price moved {deviation:.2f}% since signal (max {self.limits.max_entry_deviation_pct}%)")

        qty_by_capital = math.floor(self.limits.max_capital_per_trade / price)
        qty_by_risk = math.floor(self.limits.max_risk_per_trade / risk_per_share)
        quantity = min(qty_by_capital, qty_by_risk)
        if quantity < 1:
            return EntryDecision.reject(
                f"position size rounds to zero (capital allows {qty_by_capital}, risk allows {qty_by_risk})")

        if not self._order_limiter.try_acquire():
            return EntryDecision.reject("order rate limit reached")

        return EntryDecision(True, "approved", quantity, side, round(stop, 2),
                             round(target, 2) if target is not None else None)

    # ── Position book ────────────────────────────────────────────────────────
    def open_position(self, symbol: str, side: Side, quantity: int, fill_price: float,
                      stop_loss: float, target: float | None, now: datetime,
                      fees: float = 0.0, order_id: str | None = None,
                      trade_id: int | None = None) -> Position:
        if symbol in self.positions:
            raise RuntimeError(f"position for {symbol} already tracked")
        pos = Position(
            symbol=symbol, side=side, quantity=quantity, entry_price=fill_price,
            stop_loss=stop_loss, target=target, trailing_pct=self.limits.trailing_stop_pct / 100,
            opened_at=now, entry_fees=fees, entry_order_id=order_id, trade_id=trade_id,
        )
        self.positions[symbol] = pos
        logger.info("position opened", extra={"ctx": {
            "symbol": symbol, "side": side.value, "qty": quantity, "fill": fill_price,
            "stop": stop_loss, "target": target}})
        return pos

    def on_price(self, symbol: str, price: float) -> ExitReason | None:
        """Mark a position and return an exit reason if a stop/target is hit."""
        pos = self.positions.get(symbol)
        if pos is None or not math.isfinite(price) or price <= 0:
            return None
        # Check against the stop *before* ratcheting on this print, then ratchet.
        reason = pos.exit_trigger(price)
        pos.mark(price)
        return reason

    def close_position(self, symbol: str, fill_price: float, fees: float = 0.0,
                       quantity: int | None = None) -> float:
        """
        Book a confirmed exit fill. `quantity` smaller than the position is a
        partial exit: the remainder stays tracked and protected. Returns the
        realized PnL of this fill, net of fees (entry fees are charged to the
        first exit fill).
        """
        pos = self.positions[symbol]
        qty = pos.quantity if quantity is None else min(quantity, pos.quantity)
        if qty <= 0:
            raise ValueError("exit quantity must be positive")
        gross = (fill_price - pos.entry_price) * qty * pos.side.sign
        net = gross - pos.entry_fees - fees
        pos.entry_fees = 0.0
        pos.exit_failures = 0
        self.realized_pnl += net
        if qty < pos.quantity:
            pos.quantity -= qty
            logger.warning("partial exit", extra={"ctx": {
                "symbol": symbol, "filled": qty, "remaining": pos.quantity, "pnl": round(net, 2)}})
        else:
            del self.positions[symbol]
            self.trades_today += 1
            if net > 0:
                self.wins_today += 1
            logger.info("position closed", extra={"ctx": {
                "symbol": symbol, "fill": fill_price, "pnl": round(net, 2),
                "realized_today": round(self.realized_pnl, 2)}})
        if self.realized_pnl <= -self.limits.max_daily_loss:
            self.halt(f"daily loss limit ₹{self.limits.max_daily_loss:,.0f} reached", daily_loss=True)
        return net

    def record_exit_failure(self, symbol: str) -> int:
        pos = self.positions.get(symbol)
        if pos is None:
            return 0
        pos.exit_failures += 1
        if pos.exit_failures >= self.limits.max_consecutive_exit_failures:
            self.halt(f"{pos.exit_failures} consecutive failed exit attempts for {symbol} — manual attention needed")
        return pos.exit_failures
