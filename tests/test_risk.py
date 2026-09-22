import pytest

from algotrader.domain import Side, Signal, SignalAction
from algotrader.resilience import SlidingWindowLimiter
from algotrader.risk import RiskLimits, RiskManager
from tests.conftest import MONDAY_10AM, buy_signal, ist, make_settings, sell_signal

NOW = MONDAY_10AM


def test_approves_and_sizes_by_risk(risk):
    d = risk.evaluate_entry(buy_signal(stop=990.0, target=1020.0), 1000.0, NOW)
    assert d.approved, d.reason
    # capital allows 100 shares, ₹1000 risk / ₹10 per share allows 100 → 100
    assert d.quantity == 100
    assert d.side is Side.BUY and d.stop_loss == 990.0


def test_sizes_by_capital_when_tighter(calendar):
    r = RiskManager(RiskLimits.from_settings(make_settings(max_capital_per_trade=20_000.0)), calendar)
    d = r.evaluate_entry(buy_signal(), 1000.0, NOW)
    assert d.quantity == 20


def test_rejects_when_size_rounds_to_zero(calendar):
    r = RiskManager(RiskLimits.from_settings(
        make_settings(max_capital_per_trade=500.0, max_risk_per_trade=100.0)), calendar)
    d = r.evaluate_entry(buy_signal(), 1000.0, NOW)
    assert not d.approved and "rounds to zero" in d.reason


@pytest.mark.parametrize("signal,price,fragment", [
    (Signal("RELIANCE", SignalAction.HOLD, "t"), 1000.0, "HOLD"),
    (buy_signal(stop=None), 1000.0, "no valid stop"),
    (buy_signal(stop=1001.0), 1000.0, "not below"),
    (buy_signal(target=999.0), 1000.0, "target"),
    (sell_signal(stop=999.0), 1000.0, "not above"),
    (buy_signal(stop=990.0, target=1010.0), 1000.0, "reward:risk"),
    (buy_signal(confidence=0.3), 1000.0, "confidence"),
    (buy_signal(ref=950.0), 1000.0, "price moved"),
    (buy_signal(), float("nan"), "invalid price"),
    (buy_signal(), 0.0, "invalid price"),
])
def test_rejections(risk, signal, price, fragment):
    d = risk.evaluate_entry(signal, price, NOW)
    assert not d.approved
    assert fragment in d.reason


def test_rejects_outside_entry_window(risk):
    assert not risk.evaluate_entry(buy_signal(), 1000.0, ist(2025, 1, 6, 9, 0)).approved
    assert not risk.evaluate_entry(buy_signal(), 1000.0, ist(2025, 1, 6, 15, 0)).approved
    assert not risk.evaluate_entry(buy_signal(), 1000.0, ist(2025, 1, 4, 10, 0)).approved  # Saturday


def test_short_can_be_disabled(calendar):
    r = RiskManager(RiskLimits.from_settings(make_settings(allow_short=False)), calendar)
    assert "short" in r.evaluate_entry(sell_signal(), 1000.0, NOW).reason


def test_position_limits(risk):
    risk.open_position("A", Side.BUY, 1, 100.0, 99.0, None, NOW)
    risk.open_position("B", Side.BUY, 1, 100.0, 99.0, None, NOW)
    risk.pending.add("C")
    d = risk.evaluate_entry(buy_signal("D"), 1000.0, NOW)
    assert not d.approved and "max open positions" in d.reason
    assert "already open" in risk.evaluate_entry(buy_signal("A"), 1000.0, NOW).reason
    risk.pending.discard("C")
    risk.pending.add("D")
    assert "in flight" in risk.evaluate_entry(buy_signal("D"), 1000.0, NOW).reason


def test_order_rate_limit(settings, calendar):
    r = RiskManager(RiskLimits.from_settings(settings), calendar, order_limiter=SlidingWindowLimiter(1, 60))
    assert r.evaluate_entry(buy_signal("A"), 1000.0, NOW).approved
    assert "rate limit" in r.evaluate_entry(buy_signal("B"), 1000.0, NOW).reason


def test_close_books_net_pnl_and_halts_on_daily_loss(risk):
    risk.open_position("A", Side.BUY, 100, 1000.0, 900.0, None, NOW, fees=20.0)
    pnl = risk.close_position("A", 950.0, fees=20.0)
    assert pnl == -5000.0 - 40.0
    assert risk.halted and "daily loss" in risk.halt_reason
    assert not risk.evaluate_entry(buy_signal("B"), 1000.0, NOW).approved
    ok, _ = risk.resume()
    assert not ok, "daily-loss halt must not be resumable the same day"


def test_loss_limit_counts_unrealized(risk):
    risk.open_position("A", Side.BUY, 100, 1000.0, 900.0, None, NOW)
    risk.on_price("A", 949.0)
    assert risk.loss_limit_breached()


def test_roll_day_clears_daily_halt_but_not_manual(risk):
    risk.roll_day(NOW.date())
    risk.halt("x", daily_loss=True)
    assert risk.roll_day(ist(2025, 1, 7).date())
    assert not risk.halted
    risk.halt("manual")
    risk.roll_day(ist(2025, 1, 8).date())
    assert risk.halted
    assert risk.resume()[0]


def test_partial_close_keeps_remainder(risk):
    risk.open_position("A", Side.BUY, 10, 100.0, 95.0, None, NOW, fees=5.0)
    pnl = risk.close_position("A", 110.0, fees=1.0, quantity=4)
    assert pnl == 40.0 - 5.0 - 1.0
    assert risk.positions["A"].quantity == 6
    assert risk.positions["A"].entry_fees == 0.0
    risk.close_position("A", 110.0)
    assert "A" not in risk.positions and risk.trades_today == 1


def test_exit_failures_escalate_to_halt(risk):
    risk.open_position("A", Side.BUY, 1, 100.0, 95.0, None, NOW)
    for _ in range(3):
        risk.record_exit_failure("A")
    assert risk.halted and "failed exit" in risk.halt_reason


def test_on_price_checks_stop_before_ratcheting(calendar):
    r = RiskManager(RiskLimits.from_settings(make_settings(trailing_stop_loss_pct=1.0)), calendar)
    r.open_position("A", Side.BUY, 1, 100.0, 95.0, None, NOW)
    assert r.on_price("A", 110.0) is None
    assert r.positions["A"].stop_loss == 108.9
    assert r.on_price("A", 108.0) is not None
