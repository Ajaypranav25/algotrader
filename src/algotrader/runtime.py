"""
Composition root: builds the object graph for paper or live trading.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from algotrader.brokers.angel_client import AngelOneClient, AuthError, BrokerError
from algotrader.brokers.base import Broker
from algotrader.brokers.live import LiveBroker
from algotrader.brokers.paper import PaperBroker
from algotrader.config import Instrument, Settings, TradingMode, load_watchlist
from algotrader.data.feed import AngelTickFeed
from algotrader.domain import Candle
from algotrader.engine import TradingEngine
from algotrader.events import EventBus
from algotrader.market_calendar import Clock, MarketCalendar, SystemClock, load_holidays
from algotrader.persistence import Repository
from algotrader.risk import RiskLimits, RiskManager
from algotrader.strategies import build_strategy

logger = logging.getLogger(__name__)


class LiveMarketData:
    """Ticks from the websocket, REST quotes as a throttled fallback when ticks are stale."""

    REST_MIN_INTERVAL = 2.0

    def __init__(self, client: AngelOneClient, feed: AngelTickFeed, settings: Settings,
                 instruments: dict[str, Instrument]) -> None:
        self._client = client
        self._feed = feed
        self._settings = settings
        self.instruments = instruments
        self._rest: dict[str, tuple[float, float]] = {}  # symbol -> (price, monotonic ts)

    async def candles(self, instrument: Instrument) -> list[Candle]:
        try:
            return await self._client.get_candles(instrument, self._settings.candle_interval,
                                                  self._settings.candle_lookback_days)
        except (BrokerError, AuthError, OSError) as exc:
            logger.warning("candle fetch failed for %s: %s", instrument.symbol, exc)
            return []

    async def current_price(self, instrument: Instrument) -> float | None:
        max_age = self._settings.stale_price_seconds
        tick = self._feed.cache.get(instrument.token, max_age=max_age)
        if tick is not None:
            return tick
        cached = self._rest.get(instrument.symbol)
        now = time.monotonic()
        if cached and now - cached[1] < self.REST_MIN_INTERVAL:
            return cached[0]
        try:
            price = await self._client.get_ltp(instrument)
        except (BrokerError, AuthError, OSError) as exc:
            logger.warning("REST quote failed for %s: %s", instrument.symbol, exc)
            price = None
        if price is not None:
            self._rest[instrument.symbol] = (price, now)
            return price
        # Fall back to a slightly older quote only if it is still inside the staleness budget.
        if cached and now - cached[1] < max_age:
            return cached[0]
        return None

    def last_price(self, symbol: str) -> float | None:
        inst = self.instruments.get(symbol)
        if inst is not None:
            tick = self._feed.cache.get(inst.token, max_age=self._settings.stale_price_seconds)
            if tick is not None:
                return tick
        cached = self._rest.get(symbol)
        return cached[0] if cached else None


@dataclass
class Runtime:
    settings: Settings
    engine: TradingEngine
    client: AngelOneClient
    feed: AngelTickFeed
    repo: Repository
    bus: EventBus
    calendar: MarketCalendar
    clock: Clock
    market_data: LiveMarketData
    _loop: asyncio.AbstractEventLoop | None = None

    def relogin_threadsafe(self) -> None:
        """Called from the feed thread after repeated connection failures (usually an expired session)."""
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self.client.login(), self._loop)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self.repo.init()
        if self.settings.has_broker_credentials:
            try:
                await self.connect_broker()
            except AuthError as exc:
                logger.error("broker login failed: %s — use POST /api/v1/auth/login after fixing credentials", exc)
        else:
            logger.warning("Angel One credentials not configured — no market data available")
        # After login, so live mode can cross-check restored positions with the broker.
        await self.engine.reconcile()
        await self.engine.launch()

    async def connect_broker(self) -> None:
        await self.client.login()
        self.feed.start(self.engine.instruments.values())

    def set_watchlist(self, instruments: list[Instrument]) -> None:
        self.engine.set_instruments(instruments)
        self.market_data.instruments = dict(self.engine.instruments)
        if self.client.is_connected:
            self.feed.start(self.engine.instruments.values())

    async def stop(self) -> None:
        await self.engine.shutdown()
        await asyncio.to_thread(self.feed.stop)
        await self.client.logout()
        await self.repo.close()


def build_calendar(settings: Settings) -> MarketCalendar:
    return MarketCalendar(settings.market_open, settings.entry_cutoff, settings.square_off_time,
                          settings.market_close, load_holidays(settings.holidays_file))


def build_runtime(settings: Settings, clock: Clock | None = None) -> Runtime:
    clock = clock or SystemClock()
    calendar = build_calendar(settings)
    instruments = load_watchlist(settings.watchlist_file)
    instrument_map = {i.symbol: i for i in instruments}
    bus = EventBus()
    repo = Repository(settings.database_url, settings.mode.value)
    client = AngelOneClient(settings, clock)
    feed = AngelTickFeed(client.feed_credentials, calendar, clock)
    market_data = LiveMarketData(client, feed, settings, instrument_map)

    broker: Broker
    if settings.mode is TradingMode.LIVE:
        broker = LiveBroker(client, settings.order_fill_timeout_seconds, fee_per_order=settings.fee_per_order)
        broker_positions = client.get_positions
    else:
        broker = PaperBroker(market_data.last_price, settings.slippage_bps, settings.fee_per_order)
        broker_positions = None

    engine = TradingEngine(
        settings=settings, strategy=build_strategy(settings),
        risk=RiskManager(RiskLimits.from_settings(settings), calendar),
        broker=broker, market_data=market_data, repo=repo, bus=bus, calendar=calendar, clock=clock,
        instruments=instruments, broker_positions=broker_positions,
    )
    runtime = Runtime(settings, engine, client, feed, repo, bus, calendar, clock, market_data)
    feed.on_auth_failure = runtime.relogin_threadsafe
    return runtime
