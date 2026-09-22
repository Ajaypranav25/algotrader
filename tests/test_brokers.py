from typing import Any

import pytest

from algotrader.brokers.angel_client import completed_candles
from algotrader.brokers.live import LiveBroker
from algotrader.brokers.paper import PaperBroker
from algotrader.domain import OrderRequest, OrderStatus, Side
from tests.conftest import candles_from_closes, ist


def req(side=Side.BUY, qty=10):
    return OrderRequest("RELIANCE", "RELIANCE-EQ", "2885", "NSE", side, qty)


async def test_paper_fill_applies_slippage_and_fees():
    b = PaperBroker(lambda s: 1000.0, slippage_bps=10, fee_per_order=20)
    buy = await b.place_order(req(Side.BUY))
    sell = await b.place_order(req(Side.SELL))
    assert buy.status is OrderStatus.FILLED and buy.average_price == 1001.0 and buy.fees == 20
    assert sell.average_price == 999.0
    assert buy.order_id != sell.order_id


async def test_paper_rejects_without_price():
    b = PaperBroker(lambda s: None)
    assert (await b.place_order(req())).status is OrderStatus.REJECTED


def test_order_request_rejects_non_positive_qty():
    with pytest.raises(ValueError):
        req(qty=0)


def test_completed_candles_drops_forming_bar():
    bars = candles_from_closes([100, 101, 102], start=ist(2025, 1, 6, 9, 15))
    # at 9:50 the 9:45 bar is still forming
    assert len(completed_candles(bars, "FIFTEEN_MINUTE", ist(2025, 1, 6, 9, 50))) == 2
    assert len(completed_candles(bars, "FIFTEEN_MINUTE", ist(2025, 1, 6, 10, 0))) == 3


class FakeClient:
    def __init__(self, place: Any, states: list[Any]) -> None:
        self._place = place
        self._states = states

    async def place_market_order(self, r):
        if isinstance(self._place, Exception):
            raise self._place
        return self._place

    async def get_order_state(self, order_id, unique_id=None):
        if not self._states:
            return None
        s = self._states.pop(0)
        if isinstance(s, Exception):
            raise s
        return s


def state(status, filled=0, avg=0.0, msg=""):
    from algotrader.brokers.angel_client import BrokerOrderState

    return BrokerOrderState("O1", status, filled, avg, msg)


async def test_live_broker_confirms_fill():
    c = FakeClient(("O1", "U1", "ok"), [state("open"), OSError("net"), state("complete", 10, 1000.5)])
    res = await LiveBroker(c, fill_timeout=2, poll_interval=0.001).place_order(req())
    assert res.status is OrderStatus.FILLED and res.average_price == 1000.5 and res.filled_quantity == 10


async def test_live_broker_rejection():
    c = FakeClient(("O1", None, ""), [state("rejected", msg="RMS: margin")])
    res = await LiveBroker(c, fill_timeout=2, poll_interval=0.001).place_order(req())
    assert res.status is OrderStatus.REJECTED and "margin" in res.message


async def test_live_broker_explicit_reject_without_id():
    c = FakeClient((None, None, "AB4008 invalid"), [])
    res = await LiveBroker(c, fill_timeout=1, poll_interval=0.001).place_order(req())
    assert res.status is OrderStatus.REJECTED


async def test_live_broker_transport_error_is_unknown_not_rejected():
    c = FakeClient(TimeoutError("read timeout"), [])
    res = await LiveBroker(c, fill_timeout=1, poll_interval=0.001).place_order(req())
    assert res.status is OrderStatus.UNKNOWN


async def test_live_broker_timeout_is_unknown():
    c = FakeClient(("O1", None, ""), [state("open")] * 1000)
    res = await LiveBroker(c, fill_timeout=0.05, poll_interval=0.001).place_order(req())
    assert res.status is OrderStatus.UNKNOWN and res.order_id == "O1"


async def test_live_broker_partial_then_cancelled_counts_as_fill():
    c = FakeClient(("O1", None, ""), [state("cancelled", 4, 999.0)])
    res = await LiveBroker(c, fill_timeout=1, poll_interval=0.001).place_order(req())
    assert res.filled and res.filled_quantity == 4
