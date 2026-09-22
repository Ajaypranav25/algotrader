from datetime import timedelta

import pytest

from algotrader.domain import ExitReason, OrderResult, OrderStatus, Side
from algotrader.engine import TradingEngine
from algotrader.persistence import TradeRow
from algotrader.risk import RiskLimits, RiskManager
from tests.conftest import (
    INST,
    INST2,
    FakeBroker,
    FakeMarketData,
    ScriptedStrategy,
    buy_signal,
    candles_from_closes,
    ist,
    make_settings,
)


@pytest.fixture
def md():
    m = FakeMarketData()
    m.prices = {"RELIANCE": 1000.0, "INFY": 1500.0}
    m.bars = {"RELIANCE": candles_from_closes([1000] * 5), "INFY": candles_from_closes([1500] * 5)}
    return m


@pytest.fixture
def strategy():
    return ScriptedStrategy()


@pytest.fixture
def broker(md):
    return FakeBroker(md.last_price)


@pytest.fixture
def engine(repo, bus, calendar, clock, md, strategy, broker):
    s = make_settings()
    e = TradingEngine(s, strategy, RiskManager(RiskLimits.from_settings(s), calendar), broker, md, repo, bus,
                      calendar, clock, [INST, INST2])
    e.risk.roll_day(clock.now().date())
    e.running = True
    return e


async def test_entry_uses_fill_price_and_persists(engine, strategy, broker, repo):
    strategy.signals["RELIANCE"] = buy_signal()
    broker.queue.append(OrderResult(OrderStatus.FILLED, "O1", 100, 1000.4, 20.0))
    await engine.signal_cycle()
    pos = engine.risk.positions["RELIANCE"]
    assert pos.entry_price == 1000.4 and pos.quantity == 100 and pos.entry_fees == 20.0
    rows = await repo.open_trades()
    assert len(rows) == 1 and rows[0].entry_order_id == "O1"
    decisions = {s.symbol: s.decision for s in await repo.recent_signals()}
    assert decisions == {"RELIANCE": "approved", "INFY": "HOLD"}


async def test_rejected_entry_creates_no_position(engine, strategy, broker):
    strategy.signals["RELIANCE"] = buy_signal()
    broker.queue.append(OrderResult(OrderStatus.REJECTED, None, message="margin"))
    await engine.signal_cycle()
    assert not engine.risk.positions and not engine.risk.halted


async def test_unknown_entry_outcome_halts(engine, strategy, broker):
    strategy.signals["RELIANCE"] = buy_signal()
    strategy.signals["INFY"] = buy_signal("INFY", 1485.0, 1530.0)
    broker.queue.append(OrderResult(OrderStatus.UNKNOWN, "O1", message="timeout"))
    await engine.signal_cycle()
    assert engine.risk.halted and "unconfirmed" in engine.risk.halt_reason
    assert len(broker.orders) == 1, "no further entries after an unknown outcome"


async def test_stop_hit_exits(engine, strategy, md, repo):
    strategy.signals["RELIANCE"] = buy_signal()
    await engine.signal_cycle()
    md.prices["RELIANCE"] = 989.0
    await engine.protect_once()
    assert "RELIANCE" not in engine.risk.positions
    closed = await repo.recent_trades(status="CLOSED")
    assert closed[0].exit_reason == ExitReason.STOP_LOSS.value
    assert closed[0].pnl == pytest.approx((989.0 - 1000.0) * 100)


async def test_failed_exit_keeps_position_and_escalates(engine, strategy, md, broker):
    strategy.signals["RELIANCE"] = buy_signal()
    await engine.signal_cycle()
    md.prices["RELIANCE"] = 985.0
    for _ in range(3):
        broker.queue.append(OrderResult(OrderStatus.REJECTED, None, message="rms"))
        engine._exit_retry_at.clear()  # skip backoff wait in the test
        await engine.protect_once()
        assert "RELIANCE" in engine.risk.positions, "position must not be dropped on a failed exit"
    assert engine.risk.halted and "failed exit" in engine.risk.halt_reason
    engine._exit_retry_at.clear()
    await engine.protect_once()
    assert "RELIANCE" not in engine.risk.positions, "exits still allowed while halted"


async def test_exit_backoff_throttles_retries(engine, strategy, md, broker):
    strategy.signals["RELIANCE"] = buy_signal()
    await engine.signal_cycle()
    md.prices["RELIANCE"] = 985.0
    broker.queue.append(OrderResult(OrderStatus.REJECTED, None))
    await engine.protect_once()
    n = len(broker.orders)
    await engine.protect_once()
    assert len(broker.orders) == n


async def test_square_off_at_cutoff(engine, strategy, clock):
    strategy.signals["RELIANCE"] = buy_signal()
    await engine.signal_cycle()
    clock.set(ist(2025, 1, 6, 15, 10))
    await engine.protect_once()
    assert not engine.risk.positions


async def test_stopped_bot_still_protects_positions(engine, strategy, md):
    strategy.signals["RELIANCE"] = buy_signal()
    await engine.signal_cycle()
    engine.stop()
    md.prices["RELIANCE"] = 1025.0
    await engine.protect_once()
    assert not engine.risk.positions


async def test_no_entries_when_stopped_or_after_cutoff(engine, strategy, broker, clock):
    strategy.signals["RELIANCE"] = buy_signal()
    engine.stop()
    await engine._signal_cycle_if_running()
    engine.running = True
    clock.set(ist(2025, 1, 6, 14, 56))
    await engine.signal_cycle()
    assert not broker.orders


async def test_no_entry_without_fresh_price(engine, strategy, md, broker):
    strategy.signals["RELIANCE"] = buy_signal()
    md.prices["RELIANCE"] = None
    await engine.signal_cycle()
    assert not broker.orders


async def test_daily_loss_with_unrealized_flattens(engine, strategy, md):
    strategy.signals["RELIANCE"] = buy_signal(stop=900.0, target=1200.0)
    await engine.signal_cycle()
    qty = engine.risk.positions["RELIANCE"].quantity
    md.prices["RELIANCE"] = 1000.0 - 5001.0 / qty
    await engine.protect_once()
    assert not engine.risk.positions and engine.risk.halted


async def test_kill_switch_closes_everything(engine, strategy):
    strategy.signals["RELIANCE"] = buy_signal()
    strategy.signals["INFY"] = buy_signal("INFY", 1485.0, 1530.0)
    await engine.signal_cycle()
    assert len(engine.risk.positions) == 2
    res = await engine.kill_switch()
    assert res["success"] and res["positions_closed"] == 2
    assert engine.risk.halted and not engine.running


async def test_kill_switch_flattens_untracked_broker_positions(engine, broker, md):
    async def broker_positions():
        return [{"tradingsymbol": "TCS-EQ", "symbolname": "TCS", "symboltoken": "11536", "exchange": "NSE",
                 "netqty": "-5", "producttype": "INTRADAY"},
                {"tradingsymbol": "HDFC-EQ", "symbolname": "HDFC", "symboltoken": "1", "netqty": "3",
                 "producttype": "DELIVERY"}]
    engine._broker_positions = broker_positions
    md.prices["TCS"] = 4000.0
    res = await engine.kill_switch()
    assert res["positions_closed"] == 1
    assert broker.orders[-1].side is Side.BUY and broker.orders[-1].quantity == 5


async def test_reconcile_restores_today_and_orphans_old(engine, repo, clock):
    async with repo.session() as s, s.begin():
        s.add(TradeRow(mode="paper", trading_day=clock.now().date().isoformat(), symbol="RELIANCE", side="BUY",
                       quantity=10, entry_price=1000.0, stop_loss=995.0, initial_stop=990.0, target=1020.0,
                       fees=20.0, status="OPEN", strategy="t"))
        s.add(TradeRow(mode="paper", trading_day=(clock.now() - timedelta(days=3)).date().isoformat(),
                       symbol="INFY", side="BUY", quantity=1, entry_price=1.0, stop_loss=0.5, initial_stop=0.5,
                       status="OPEN", strategy="t"))
    await engine.reconcile()
    pos = engine.risk.positions["RELIANCE"]
    assert pos.quantity == 10 and pos.stop_loss == 995.0 and pos.initial_stop == 990.0
    assert "INFY" not in engine.risk.positions
    assert [t.symbol for t in await repo.recent_trades(status="ORPHANED")] == ["INFY"]


async def test_reconcile_halts_on_broker_mismatch(engine, repo, clock):
    async with repo.session() as s, s.begin():
        s.add(TradeRow(mode="paper", trading_day=clock.now().date().isoformat(), symbol="RELIANCE", side="BUY",
                       quantity=10, entry_price=1000.0, stop_loss=990.0, initial_stop=990.0, status="OPEN",
                       strategy="t"))

    async def broker_positions():
        return [{"symbolname": "RELIANCE", "netqty": "0"}]
    engine._broker_positions = broker_positions
    await engine.reconcile()
    assert engine.risk.halted and "mismatch" in engine.risk.halt_reason


async def test_events_published(engine, strategy, bus):
    async with bus.subscribe() as q:
        strategy.signals["RELIANCE"] = buy_signal()
        await engine.signal_cycle()
        events = [q.get_nowait()["event"] for _ in range(q.qsize())]
    assert "gemini_signal" in events and "trade_opened" in events
