import asyncio
import json
import random
from datetime import date, time

import pytest

from algotrader.market_calendar import MarketCalendar, load_holidays
from algotrader.resilience import ExponentialBackoff, SlidingWindowLimiter, retry_async
from tests.conftest import ist


def cal(holidays=frozenset()):
    return MarketCalendar(time(9, 15), time(14, 55), time(15, 10), time(15, 30), holidays)


def test_session_windows():
    c = cal()
    assert not c.is_open(ist(2025, 1, 6, 9, 14))
    assert c.is_open(ist(2025, 1, 6, 9, 15))
    assert c.entries_allowed(ist(2025, 1, 6, 14, 54))
    assert not c.entries_allowed(ist(2025, 1, 6, 14, 55))
    assert not c.must_square_off(ist(2025, 1, 6, 15, 9))
    assert c.must_square_off(ist(2025, 1, 6, 15, 10))
    assert c.must_square_off(ist(2025, 1, 6, 8, 0))
    assert not c.is_open(ist(2025, 1, 5, 11, 0))  # Sunday


def test_holidays(tmp_path):
    p = tmp_path / "h.json"
    p.write_text(json.dumps({"holidays": [{"date": "2025-01-06", "name": "x"}]}))
    c = cal(load_holidays(p))
    assert not c.is_open(ist(2025, 1, 6, 10, 0))
    assert c.must_square_off(ist(2025, 1, 6, 10, 0))
    assert load_holidays(tmp_path / "missing.json") == frozenset()
    assert date(2025, 1, 6) in load_holidays(p)


def test_backoff_grows_and_caps():
    b = ExponentialBackoff(base=1, cap=8, rng=random.Random(0))
    delays = [b.next_delay() for _ in range(6)]
    assert all(0.5 <= d <= 8 for d in delays)
    assert delays[-1] >= 4  # capped window is [4, 8]
    b.reset()
    assert b.next_delay() <= 1


async def test_retry_async_retries_then_succeeds():
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise OSError("boom")
        return "ok"

    assert await retry_async(flaky, attempts=3, base_delay=0.001, max_delay=0.002) == "ok"
    assert calls == 3


async def test_retry_async_does_not_retry_unlisted_errors():
    async def bad():
        raise KeyError("x")

    with pytest.raises(KeyError):
        await retry_async(bad, retry_on=(OSError,), base_delay=0.001)


def test_sliding_window_limiter():
    t = [0.0]
    lim = SlidingWindowLimiter(2, 10.0, clock=lambda: t[0])
    assert lim.try_acquire() and lim.try_acquire()
    assert not lim.try_acquire()
    t[0] = 10.0
    assert lim.try_acquire()


async def test_event_bus_fanout_and_drop_oldest():
    from algotrader.events import EventBus

    bus = EventBus(maxsize=2)
    async with bus.subscribe() as a, bus.subscribe() as b:
        for i in range(3):
            bus.publish("e", {"i": i})
        assert [a.get_nowait()["data"]["i"] for _ in range(2)] == [1, 2]
        assert b.qsize() == 2
    assert bus.subscriber_count == 0
    await asyncio.sleep(0)
