"""Small dependency-free indicator helpers."""
from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise

from algotrader.domain import Candle


def ema(values: Sequence[float], period: int) -> list[float]:
    """EMA seeded with the SMA of the first `period` values. Output aligns with input from index period-1."""
    if period < 1:
        raise ValueError("period must be >= 1")
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def atr(candles: Sequence[Candle], period: int) -> float | None:
    """Wilder's ATR of the last bar."""
    if len(candles) < period + 1:
        return None
    trs = [
        max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
        for p, c in pairwise(candles)
    ]
    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value
