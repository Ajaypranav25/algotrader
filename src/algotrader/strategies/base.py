"""Strategy interface. Strategies are pure signal generators — they never size or place orders."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from algotrader.config import Instrument
from algotrader.domain import Candle, Signal, SignalAction


class Strategy(ABC):
    name: str = "base"
    #: minimum number of completed candles needed before generate() can act
    warmup: int = 1

    @abstractmethod
    async def generate(self, instrument: Instrument, candles: Sequence[Candle], ltp: float | None) -> Signal:
        """
        Produce a signal from *completed* candles (oldest first) and the latest price.
        Must not raise for bad/insufficient data — return HOLD instead.
        """

    def hold(self, instrument: Instrument, reason: str, price: float | None = None) -> Signal:
        return Signal(instrument.symbol, SignalAction.HOLD, self.name, rationale=reason, reference_price=price)
