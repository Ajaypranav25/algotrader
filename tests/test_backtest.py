from collections.abc import Sequence

import pytest

from algotrader.backtest import Backtester, _intrabar_exit
from algotrader.config import Instrument
from algotrader.domain import Candle, ExitReason, Side, Signal, SignalAction
from algotrader.strategies.base import Strategy
from algotrader.strategies.ema_crossover import EmaCrossoverStrategy
from tests.conftest import candles_from_closes, ist, make_settings


class BuyOnceAt(Strategy):
    """Emits one BUY on the bar at index `at` with fixed levels."""

    name = "buy_once"
    warmup = 1

    def __init__(self, at: int, stop: float, target: float) -> None:
        self.at, self.stop, self.target = at, stop, target
        self.seen = 0

    async def generate(self, instrument: Instrument, candles: Sequence[Candle], ltp: float | None) -> Signal:
        if len(candles) - 1 == self.at:
            return Signal(instrument.symbol, SignalAction.BUY, self.name, stop_loss=self.stop, target=self.target,
                          reference_price=ltp)
        return self.hold(instrument, "", ltp)


def bars(closes):
    return candles_from_closes(closes, start=ist(2025, 1, 6, 9, 15), spread=0.0)


async def test_entry_fills_at_next_bar_open_no_lookahead(calendar):
    data = bars([1000, 1000, 1005, 1010, 1030, 1030])
    bt = Backtester(make_settings(min_reward_risk=0), BuyOnceAt(1, 990.0, 1025.0), calendar)
    res = await bt.run({"X": data})
    t = res.trades[0]
    assert t.entry_time == data[2].timestamp
    assert t.entry_price == data[2].open  # zero slippage in tests
    assert t.exit_reason is ExitReason.TARGET
    assert t.exit_price == 1025.0


async def test_stop_wins_when_both_inside_bar():
    bar = Candle(ist(2025, 1, 6, 10), 1000, 1030, 980, 1000, 1)
    assert _intrabar_exit(Side.BUY, 990.0, 1020.0, bar) == (990.0, True)
    gap = Candle(ist(2025, 1, 6, 10), 985, 1000, 980, 990, 1)
    assert _intrabar_exit(Side.BUY, 990.0, 1020.0, gap) == (985, True)
    short = Candle(ist(2025, 1, 6, 10), 1000, 1015, 990, 1000, 1)
    assert _intrabar_exit(Side.SELL, 1010.0, 980.0, short) == (1010.0, True)


async def test_positions_squared_off_same_day(calendar):
    # 26 bars = full 9:15–15:30 session; position never hits stop/target.
    data = bars([1000.0] * 26)
    res = await Backtester(make_settings(min_reward_risk=0), BuyOnceAt(2, 900.0, 1500.0), calendar).run({"X": data})
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.exit_reason is ExitReason.SQUARE_OFF
    assert t.exit_time.date() == t.entry_time.date()
    assert t.exit_time.time() >= calendar.square_off


async def test_fees_and_slippage_reduce_pnl(calendar):
    data = bars([1000, 1000, 1000, 1000, 1000, 1000])
    s = make_settings(min_reward_risk=0, slippage_bps=10, fee_per_order=20)
    res = await Backtester(s, BuyOnceAt(1, 900.0, 2000.0), calendar).run({"X": data})
    t = res.trades[0]
    assert t.exit_reason is ExitReason.END_OF_DATA
    assert t.pnl == pytest.approx((999.0 - 1001.0) * t.quantity - 40.0)


async def test_ema_backtest_runs_on_sample_data(tmp_path, calendar):
    from algotrader.cli import main
    from algotrader.data.csv_source import load_candles_csv

    out = tmp_path / "S.csv"
    assert main(["sample-data", "--out", str(out), "--days", "20", "--seed", "3"]) == 0
    candles = load_candles_csv(out)
    res = await Backtester(make_settings(fee_per_order=20, slippage_bps=2), EmaCrossoverStrategy(),
                           calendar).run({"S": candles})
    assert res.signals > 0 and res.trades
    for t in res.trades:
        assert t.exit_time.date() == t.entry_time.date(), "intraday positions never held overnight"
    assert res.max_drawdown >= 0
    assert res.summary()["trades"] == len(res.trades)
    assert res.equity_curve[-1][1] == pytest.approx(res.net_pnl)
