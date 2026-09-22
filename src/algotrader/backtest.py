"""
Event-driven bar backtester.

Uses the *same* Strategy, RiskManager and PaperBroker as live trading so that
risk rules and sizing are identical. Assumptions (all conservative):

* Signals are generated on the close of bar i from bars[0..i] only, and filled
  at the OPEN of bar i+1 (no look-ahead). The risk gate is evaluated against
  that fill price.
* Intrabar exits: if the bar gaps through the stop the fill is the open; if both
  stop and target lie inside one bar the STOP is assumed to hit first.
* The trailing stop ratchets on the bar's favourable extreme only after the
  bar's stop/target checks, so a bar can never both raise the stop and hit it.
* Positions are squared off at the first bar at/after `square_off_time`, and
  anything still open at the end of the data is closed at the last close.
* Slippage and a flat per-order fee are applied to every fill.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from algotrader.brokers.paper import PaperBroker
from algotrader.config import Instrument, Settings
from algotrader.domain import Candle, ExitReason, OrderRequest, Side, Signal, SignalAction
from algotrader.market_calendar import MarketCalendar
from algotrader.resilience import SlidingWindowLimiter
from algotrader.risk import RiskLimits, RiskManager
from algotrader.strategies.base import Strategy


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    symbol: str
    side: Side
    quantity: int
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    pnl: float
    exit_reason: ExitReason


@dataclass
class BacktestResult:
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    rejected_signals: dict[str, int] = field(default_factory=dict)
    signals: int = 0

    @property
    def net_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def win_rate(self) -> float:
        return sum(t.pnl > 0 for t in self.trades) / len(self.trades) if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl for t in self.trades if t.pnl > 0)
        losses = -sum(t.pnl for t in self.trades if t.pnl < 0)
        if losses == 0:
            return math.inf if gains > 0 else 0.0
        return gains / losses

    @property
    def max_drawdown(self) -> float:
        peak, mdd = 0.0, 0.0
        for _, equity in self.equity_curve:
            peak = max(peak, equity)
            mdd = max(mdd, peak - equity)
        return mdd

    @property
    def avg_trade(self) -> float:
        return self.net_pnl / len(self.trades) if self.trades else 0.0

    def summary(self) -> dict[str, float | int]:
        return {
            "trades": len(self.trades),
            "signals": self.signals,
            "net_pnl": round(self.net_pnl, 2),
            "win_rate_pct": round(self.win_rate * 100, 1),
            "profit_factor": round(self.profit_factor, 2) if math.isfinite(self.profit_factor) else float("inf"),
            "avg_trade": round(self.avg_trade, 2),
            "max_drawdown": round(self.max_drawdown, 2),
        }


class _BarPrices:
    def __init__(self) -> None:
        self.price: dict[str, float] = {}

    def __call__(self, symbol: str) -> float | None:
        return self.price.get(symbol)


class Backtester:
    def __init__(self, settings: Settings, strategy: Strategy, calendar: MarketCalendar,
                 max_history: int = 300) -> None:
        self.settings = settings
        self.strategy = strategy
        self.calendar = calendar
        self.max_history = max_history

    async def run(self, data: Mapping[str, Sequence[Candle]], instruments: Mapping[str, Instrument] | None = None
                  ) -> BacktestResult:
        prices = _BarPrices()
        broker = PaperBroker(prices, self.settings.slippage_bps, self.settings.fee_per_order)
        # Wall-clock order-rate limiting is meaningless in simulated time.
        risk = RiskManager(RiskLimits.from_settings(self.settings), self.calendar,
                           order_limiter=SlidingWindowLimiter(10**9, 60.0))
        result = BacktestResult()
        insts = instruments or {s: Instrument(symbol=s, token="0") for s in data}  # noqa: S106 (instrument token)

        timeline = sorted({(c.timestamp, sym) for sym, bars in data.items() for c in bars})
        index = {sym: {c.timestamp: i for i, c in enumerate(bars)} for sym, bars in data.items()}
        pending_entry: dict[str, tuple[Side, float, float | None]] = {}
        entry_meta: dict[str, datetime] = {}

        async def fill(sym: str, side: Side, qty: int, price: float) -> tuple[float, float]:
            prices.price[sym] = price
            inst = insts[sym]
            res = await broker.place_order(OrderRequest(sym, inst.broker_symbol, inst.token, inst.exchange, side, qty))
            return res.average_price, res.fees

        async def close(sym: str, price: float, when: datetime, reason: ExitReason) -> None:
            pos = risk.positions[sym]
            px, fees = await fill(sym, pos.side.opposite, pos.quantity, price)
            side, qty, entry = pos.side, pos.quantity, pos.entry_price
            pnl = risk.close_position(sym, px, fees)
            result.trades.append(BacktestTrade(sym, side, qty, entry_meta.pop(sym), entry, when, px, pnl, reason))

        for ts, sym in timeline:
            bars = data[sym]
            i = index[sym][ts]
            bar = bars[i]
            risk.roll_day(ts.date())

            # 1) fill yesterday's-bar signal at this bar's open
            if sym in pending_entry:
                side, stop, target = pending_entry.pop(sym)
                sig_like = Signal(sym, SignalAction(side.value), self.strategy.name, stop_loss=stop, target=target)
                decision = risk.evaluate_entry(sig_like, bar.open, ts)
                if decision.approved:
                    px, fees = await fill(sym, side, decision.quantity, bar.open)
                    risk.open_position(sym, side, decision.quantity, px, decision.stop_loss, decision.target,
                                       ts, fees=fees)
                    entry_meta[sym] = ts
                else:
                    result.rejected_signals[decision.reason] = result.rejected_signals.get(decision.reason, 0) + 1

            # 2) manage the open position through this bar
            pos = risk.positions.get(sym)
            if pos is not None:
                if self.calendar.must_square_off(ts):
                    await close(sym, bar.open, ts, ExitReason.SQUARE_OFF)
                else:
                    exit_at = _intrabar_exit(pos.side, pos.stop_loss, pos.target, bar)
                    if exit_at is not None:
                        price, hit_stop = exit_at
                        reason = ((ExitReason.TRAILING_STOP if pos.stop_has_trailed else ExitReason.STOP_LOSS)
                                  if hit_stop else ExitReason.TARGET)
                        await close(sym, price, ts, reason)
                    else:
                        pos.mark(bar.high if pos.side is Side.BUY else bar.low)
                        pos.mark(bar.close)

            # 3) daily loss limit including open risk marked at the close
            if risk.positions and risk.loss_limit_breached():
                risk.halt("daily loss limit", daily_loss=True)
                for s in list(risk.positions):
                    await close(s, risk.positions[s].last_price, ts, ExitReason.DAILY_LOSS_LIMIT)

            # 4) generate a signal on this bar's close for the next bar
            if sym not in risk.positions and i + 1 < len(bars):
                history = bars[max(0, i + 1 - self.max_history): i + 1]
                if len(history) >= self.strategy.warmup:
                    signal = await self.strategy.generate(insts[sym], history, bar.close)
                    if signal.side is not None and signal.stop_loss is not None:
                        result.signals += 1
                        pending_entry[sym] = (signal.side, signal.stop_loss, signal.target)

            result.equity_curve.append((ts, risk.realized_pnl + risk.unrealized_pnl))

        for sym in list(risk.positions):
            last = data[sym][-1]
            await close(sym, last.close, last.timestamp, ExitReason.END_OF_DATA)
        if result.equity_curve:
            result.equity_curve.append((result.equity_curve[-1][0], result.net_pnl))
        return result


def _intrabar_exit(side: Side, stop: float, target: float | None, bar: Candle) -> tuple[float, bool] | None:
    """Return (fill price, hit_stop) if the bar crosses stop or target. Stop wins ties."""
    if side is Side.BUY:
        if bar.low <= stop:
            return (min(bar.open, stop), True)
        if target is not None and bar.high >= target:
            return (max(bar.open, target), False)
    else:
        if bar.high >= stop:
            return (max(bar.open, stop), True)
        if target is not None and bar.low <= target:
            return (min(bar.open, target), False)
    return None

