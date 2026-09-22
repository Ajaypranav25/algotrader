"""
Deterministic baseline: EMA crossover with ATR stops.

Enters on a fresh fast/slow EMA cross confirmed by the close, places the stop
`atr_stop_mult` ATRs away and the target at `reward_risk` times the risk.
Mainly a reproducible benchmark for the backtester and a fallback when no LLM
is configured — not a claim of edge.
"""
from __future__ import annotations

from collections.abc import Sequence

from algotrader.config import Instrument
from algotrader.domain import Candle, Signal, SignalAction
from algotrader.strategies.base import Strategy
from algotrader.strategies.indicators import atr, ema


class EmaCrossoverStrategy(Strategy):
    name = "ema_crossover"

    def __init__(self, fast: int = 9, slow: int = 21, atr_period: int = 14,
                 atr_stop_mult: float = 1.5, reward_risk: float = 2.0) -> None:
        if not 1 <= fast < slow:
            raise ValueError("require 1 <= fast < slow")
        self.fast, self.slow = fast, slow
        self.atr_period = atr_period
        self.atr_stop_mult = atr_stop_mult
        self.reward_risk = reward_risk
        self.warmup = max(slow, atr_period) + 2

    async def generate(self, instrument: Instrument, candles: Sequence[Candle], ltp: float | None) -> Signal:
        if len(candles) < self.warmup:
            return self.hold(instrument, f"warming up ({len(candles)}/{self.warmup} candles)")
        closes = [c.close for c in candles]
        fast, slow = ema(closes, self.fast), ema(closes, self.slow)
        # Align the two series on their last two points.
        f_prev, f_now = fast[-2], fast[-1]
        s_prev, s_now = slow[-2], slow[-1]
        vol = atr(candles, self.atr_period)
        close = closes[-1]
        if not vol or vol <= 0:
            return self.hold(instrument, "ATR unavailable", close)

        risk = self.atr_stop_mult * vol
        if f_prev <= s_prev and f_now > s_now and close > s_now:
            return Signal(instrument.symbol, SignalAction.BUY, self.name,
                          stop_loss=round(close - risk, 2), target=round(close + self.reward_risk * risk, 2),
                          rationale=f"EMA{self.fast} crossed above EMA{self.slow}; ATR {vol:.2f}",
                          reference_price=close)
        if f_prev >= s_prev and f_now < s_now and close < s_now:
            return Signal(instrument.symbol, SignalAction.SELL, self.name,
                          stop_loss=round(close + risk, 2), target=round(close - self.reward_risk * risk, 2),
                          rationale=f"EMA{self.fast} crossed below EMA{self.slow}; ATR {vol:.2f}",
                          reference_price=close)
        return self.hold(instrument, "no crossover", close)
