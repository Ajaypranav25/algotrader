from algotrader.domain import ExitReason, Position, Side
from tests.conftest import MONDAY_10AM


def pos(side=Side.BUY, stop=990.0, target=1030.0, trail=0.01):
    return Position("X", side, 10, 1000.0, stop, target, trail, MONDAY_10AM)


def test_trailing_does_not_engage_before_favourable_move():
    p = pos()
    p.mark(995.0)
    assert p.stop_loss == 990.0


def test_buy_trailing_ratchets_up_and_never_down():
    p = pos()
    p.mark(1010.0)
    assert p.stop_loss == 999.9  # 1010 * 0.99
    p.mark(1005.0)
    assert p.stop_loss == 999.9
    p.mark(1020.0)
    assert p.stop_loss == 1009.8


def test_trailing_never_loosens_a_tighter_initial_stop():
    p = pos(stop=1005.0, trail=0.05)
    p.mark(1010.0)  # 1010 * 0.95 = 959.5 < 1005
    assert p.stop_loss == 1005.0


def test_sell_trailing_ratchets_down():
    p = pos(side=Side.SELL, stop=1010.0, target=970.0)
    p.mark(990.0)
    assert p.stop_loss == 999.9
    p.mark(995.0)
    assert p.stop_loss == 999.9


def test_exit_reasons():
    p = pos()
    assert p.exit_trigger(1000.0) is None
    assert p.exit_trigger(990.0) is ExitReason.STOP_LOSS
    assert p.exit_trigger(1030.0) is ExitReason.TARGET
    p.mark(1010.0)
    assert p.exit_trigger(999.0) is ExitReason.TRAILING_STOP


def test_short_exit_reasons():
    p = pos(side=Side.SELL, stop=1010.0, target=970.0)
    assert p.exit_trigger(1011.0) is ExitReason.STOP_LOSS
    assert p.exit_trigger(969.0) is ExitReason.TARGET


def test_unrealized_pnl_includes_entry_fees():
    p = pos()
    p.entry_fees = 20.0
    assert p.unrealized_pnl(1010.0) == 100.0 - 20.0
    s = pos(side=Side.SELL, stop=1010.0, target=970.0)
    assert s.unrealized_pnl(990.0) == 100.0
