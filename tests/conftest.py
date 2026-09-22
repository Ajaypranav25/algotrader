from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from algotrader.config import Instrument, Settings
from algotrader.domain import Candle, OrderRequest, OrderResult, OrderStatus, Signal, SignalAction
from algotrader.events import EventBus
from algotrader.market_calendar import IST, ManualClock, MarketCalendar
from algotrader.persistence import Repository
from algotrader.risk import RiskLimits, RiskManager
from algotrader.strategies.base import Strategy

TOKEN = "t" * 40


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "strategy": "ema_crossover",
        "api_auth_token": TOKEN,
        "max_capital_per_trade": 100_000.0,
        "max_risk_per_trade": 1_000.0,
        "max_daily_loss": 5_000.0,
        "max_open_positions": 3,
        "trailing_stop_loss_pct": 0.0,
        "min_confidence": 0.55,
        "min_reward_risk": 1.5,
        "max_entry_deviation_pct": 1.0,
        "slippage_bps": 0.0,
        "fee_per_order": 0.0,
        "holidays_file": Path("does-not-exist.json"),
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


def ist(y: int, m: int, d: int, hh: int = 10, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=IST)


# A Monday.
MONDAY_10AM = ist(2025, 1, 6, 10, 0)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(MONDAY_10AM)


@pytest.fixture
def calendar() -> MarketCalendar:
    return MarketCalendar(time(9, 15), time(14, 55), time(15, 10), time(15, 30))


@pytest.fixture
def risk(settings: Settings, calendar: MarketCalendar) -> RiskManager:
    return RiskManager(RiskLimits.from_settings(settings), calendar)


INST = Instrument(symbol="RELIANCE", token="2885")
INST2 = Instrument(symbol="INFY", token="1594")


def buy_signal(symbol: str = "RELIANCE", stop: float = 990.0, target: float = 1020.0,
               confidence: float | None = 0.8, ref: float | None = None) -> Signal:
    return Signal(symbol, SignalAction.BUY, "test", stop_loss=stop, target=target, confidence=confidence,
                  reference_price=ref)


def sell_signal(symbol: str = "RELIANCE", stop: float = 1010.0, target: float = 980.0) -> Signal:
    return Signal(symbol, SignalAction.SELL, "test", stop_loss=stop, target=target, confidence=0.8)


def candles_from_closes(closes: Sequence[float], start: datetime = ist(2025, 1, 6, 9, 15),
                        minutes: int = 15, spread: float = 0.002) -> list[Candle]:
    out, ts, prev = [], start, closes[0]
    for c in closes:
        o = prev
        out.append(Candle(ts, o, max(o, c) * (1 + spread), min(o, c) * (1 - spread), c, 1000))
        prev, ts = c, ts + timedelta(minutes=minutes)
    return out


class FakeBroker:
    """Scripted broker: fills at the given price unless a result is queued."""

    name = "fake"

    def __init__(self, price: Callable[[str], float | None]) -> None:
        self._price = price
        self.queue: list[OrderResult] = []
        self.orders: list[OrderRequest] = []

    async def place_order(self, req: OrderRequest) -> OrderResult:
        self.orders.append(req)
        if self.queue:
            return self.queue.pop(0)
        px = self._price(req.symbol)
        if px is None:
            return OrderResult(OrderStatus.REJECTED, None, message="no price")
        return OrderResult(OrderStatus.FILLED, f"F{len(self.orders)}", req.quantity, px, 0.0)


class FakeMarketData:
    def __init__(self) -> None:
        self.prices: dict[str, float | None] = {}
        self.bars: dict[str, list[Candle]] = {}

    async def candles(self, instrument: Instrument) -> list[Candle]:
        return self.bars.get(instrument.symbol, [])

    async def current_price(self, instrument: Instrument) -> float | None:
        return self.prices.get(instrument.symbol)

    def last_price(self, symbol: str) -> float | None:
        return self.prices.get(symbol)


class ScriptedStrategy(Strategy):
    name = "scripted"
    warmup = 1

    def __init__(self) -> None:
        self.signals: dict[str, Signal] = {}

    async def generate(self, instrument: Instrument, candles: Sequence[Candle], ltp: float | None) -> Signal:
        return self.signals.get(instrument.symbol) or self.hold(instrument, "none", ltp)


@pytest.fixture
async def repo(tmp_path: Path) -> Any:
    r = Repository(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "paper")
    await r.init()
    yield r
    await r.close()


@pytest.fixture
def bus() -> EventBus:
    return EventBus()
