"""
Core domain types shared by every layer. No I/O here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


class SignalAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class OrderStatus(StrEnum):
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    # The broker accepted the request but we could not confirm the outcome.
    # Must be treated as "may have filled" — never as "did not fill".
    UNKNOWN = "UNKNOWN"


class ExitReason(StrEnum):
    STOP_LOSS = "STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    TARGET = "TARGET"
    SQUARE_OFF = "SQUARE_OFF"
    KILL_SWITCH = "KILL_SWITCH"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    END_OF_DATA = "END_OF_DATA"


@dataclass(frozen=True, slots=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    def __post_init__(self) -> None:
        if not (self.low <= min(self.open, self.close) and self.high >= max(self.open, self.close)):
            raise ValueError(f"inconsistent OHLC at {self.timestamp}: {self}")
        if self.low <= 0:
            raise ValueError(f"non-positive price at {self.timestamp}")


@dataclass(frozen=True, slots=True)
class Signal:
    symbol: str
    action: SignalAction
    strategy: str
    stop_loss: float | None = None
    target: float | None = None
    confidence: float | None = None
    rationale: str = ""
    reference_price: float | None = None  # price the strategy saw when deciding

    @property
    def side(self) -> Side | None:
        return None if self.action is SignalAction.HOLD else Side(self.action.value)


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    broker_symbol: str
    token: str
    exchange: str
    side: Side
    quantity: int
    tag: str = ""  # idempotency / audit tag

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("order quantity must be positive")


@dataclass(frozen=True, slots=True)
class OrderResult:
    status: OrderStatus
    order_id: str | None
    filled_quantity: int = 0
    average_price: float = 0.0
    fees: float = 0.0
    message: str = ""

    @property
    def filled(self) -> bool:
        return self.status is OrderStatus.FILLED and self.filled_quantity > 0


@dataclass(slots=True)
class Position:
    """An open position plus its protective levels. Mutated only by RiskManager."""

    symbol: str
    side: Side
    quantity: int
    entry_price: float
    stop_loss: float
    target: float | None
    trailing_pct: float  # fraction, e.g. 0.005
    opened_at: datetime
    entry_fees: float = 0.0
    trade_id: int | None = None
    entry_order_id: str | None = None
    peak_price: float = field(init=False)
    last_price: float = field(init=False)
    initial_stop: float = field(init=False)
    exit_failures: int = 0

    def __post_init__(self) -> None:
        self.peak_price = self.entry_price
        self.last_price = self.entry_price
        self.initial_stop = self.stop_loss

    def unrealized_pnl(self, price: float | None = None) -> float:
        """Mark-to-market PnL net of the entry fee already paid."""
        px = self.last_price if price is None else price
        return (px - self.entry_price) * self.quantity * self.side.sign - self.entry_fees

    def mark(self, price: float) -> None:
        """
        Record a new price and ratchet the trailing stop.

        The trail engages only once price has moved in our favour past entry, and
        the stop only ever tightens — it is never loosened.
        """
        self.last_price = price
        if self.side is Side.BUY:
            self.peak_price = max(self.peak_price, price)
            if self.trailing_pct > 0 and self.peak_price > self.entry_price:
                self.stop_loss = max(self.stop_loss, round(self.peak_price * (1 - self.trailing_pct), 2))
        else:
            self.peak_price = min(self.peak_price, price)
            if self.trailing_pct > 0 and self.peak_price < self.entry_price:
                self.stop_loss = min(self.stop_loss, round(self.peak_price * (1 + self.trailing_pct), 2))

    @property
    def stop_has_trailed(self) -> bool:
        return self.stop_loss != self.initial_stop

    def exit_trigger(self, price: float) -> ExitReason | None:
        """Exit condition at `price`, checked against the current (already ratcheted) stop."""
        stop_reason = ExitReason.TRAILING_STOP if self.stop_has_trailed else ExitReason.STOP_LOSS
        if self.side is Side.BUY:
            if price <= self.stop_loss:
                return stop_reason
            if self.target is not None and price >= self.target:
                return ExitReason.TARGET
        else:
            if price >= self.stop_loss:
                return stop_reason
            if self.target is not None and price <= self.target:
                return ExitReason.TARGET
        return None
