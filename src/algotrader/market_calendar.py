"""
Clock and NSE session calendar.

Everything time-dependent takes a `Clock`, so the live engine uses wall-clock
IST while the backtester and tests drive a simulated clock.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(IST)


class ManualClock:
    """Settable clock for tests and backtests."""

    def __init__(self, start: datetime) -> None:
        self._now = start if start.tzinfo else start.replace(tzinfo=IST)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value if value.tzinfo else value.replace(tzinfo=IST)

    def advance(self, **kwargs: float) -> None:
        self._now += timedelta(**kwargs)


def load_holidays(path: Path) -> frozenset[date]:
    if not path.exists():
        logger.warning("holiday file %s not found — only weekends will be treated as closed", path)
        return frozenset()
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("holidays", raw) if isinstance(raw, dict) else raw
    return frozenset(date.fromisoformat(d["date"] if isinstance(d, dict) else d) for d in entries)


class MarketCalendar:
    def __init__(
        self,
        market_open: time,
        entry_cutoff: time,
        square_off: time,
        market_close: time,
        holidays: frozenset[date] = frozenset(),
    ) -> None:
        self.market_open = market_open
        self.entry_cutoff = entry_cutoff
        self.square_off = square_off
        self.market_close = market_close
        self.holidays = holidays

    @staticmethod
    def _ist(now: datetime) -> datetime:
        return now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def is_open(self, now: datetime) -> bool:
        now = self._ist(now)
        return self.is_trading_day(now.date()) and self.market_open <= now.time() < self.market_close

    def entries_allowed(self, now: datetime) -> bool:
        now = self._ist(now)
        return self.is_trading_day(now.date()) and self.market_open <= now.time() < self.entry_cutoff

    def must_square_off(self, now: datetime) -> bool:
        """True from the square-off time until the next session opens."""
        now = self._ist(now)
        if not self.is_trading_day(now.date()):
            return True
        return not (self.market_open <= now.time() < self.square_off)

    def minutes_to_close(self, now: datetime) -> int:
        now = self._ist(now)
        close = datetime.combine(now.date(), self.market_close, IST)
        return max(0, int((close - now).total_seconds() // 60))
