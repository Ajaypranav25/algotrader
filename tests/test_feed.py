import threading
import time as _time
from datetime import time
from typing import ClassVar

from algotrader.config import Instrument
from algotrader.data.feed import AngelTickFeed, PriceCache, build_token_list, parse_tick
from algotrader.market_calendar import ManualClock, MarketCalendar
from algotrader.resilience import ExponentialBackoff
from tests.conftest import MONDAY_10AM


def test_parse_tick_converts_paise():
    assert parse_tick({"token": "2885", "last_traded_price": 245050}) == ("2885", 2450.5)
    assert parse_tick(b"\x01\x02") is None
    assert parse_tick({"token": "1"}) is None
    assert parse_tick({"token": "1", "last_traded_price": 0}) is None


def test_token_list_groups_by_exchange():
    items = [Instrument(symbol="A", token="1"), Instrument(symbol="B", token="2"),
             Instrument(symbol="C", token="3", exchange="BSE")]
    assert build_token_list(items) == [{"exchangeType": 1, "tokens": ["1", "2"]},
                                       {"exchangeType": 3, "tokens": ["3"]}]


def test_price_cache_staleness():
    t = [100.0]
    c = PriceCache(clock=lambda: t[0])
    c.update("1", 50.0)
    assert c.get("1", max_age=5) == 50.0
    t[0] = 106.0
    assert c.get("1", max_age=5) is None
    assert c.get("1") == 50.0


class FakeWS:
    instances: ClassVar[list["FakeWS"]] = []

    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.subscribed = []
        self.closed = threading.Event()
        self.on_open = self.on_data = self.on_error = self.on_close = None
        FakeWS.instances.append(self)

    def connect(self):
        if self.fail:
            raise ConnectionError("refused")
        self.on_open(None)
        self.on_data(None, {"token": "2885", "last_traded_price": 100_00})
        self.closed.wait(5)
        self.on_close(None)

    def subscribe(self, corr, mode, tokens):
        self.subscribed.append(tokens)

    def close_connection(self):
        self.closed.set()


def test_feed_reconnects_with_backoff_and_resubscribes():
    FakeWS.instances.clear()
    attempts = iter([True, True, False])  # two failures then a good connection
    feed = AngelTickFeed(
        credentials=lambda: object(),  # type: ignore[arg-type,return-value]
        calendar=MarketCalendar(time(9, 15), time(14, 55), time(15, 10), time(15, 30)),
        clock=ManualClock(MONDAY_10AM),
        backoff=ExponentialBackoff(base=0.01, cap=0.02),
        ws_factory=lambda creds: FakeWS(next(attempts, False)),
    )
    feed.start([Instrument(symbol="RELIANCE", token="2885")])
    deadline = _time.monotonic() + 5
    while feed.cache.get("2885") is None and _time.monotonic() < deadline:
        _time.sleep(0.01)
    try:
        assert feed.cache.get("2885") == 100.0
        assert feed.connected
        assert feed.reconnects == 2
        good = FakeWS.instances[2]
        assert good.subscribed == [[{"exchangeType": 1, "tokens": ["2885"]}]]
    finally:
        feed.stop()
    assert not feed.connected
