"""
Retry / backoff / rate-limit primitives.
"""
from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")
logger = logging.getLogger(__name__)


class ExponentialBackoff:
    """Full-jitter exponential backoff (AWS style). Thread-safe enough for one owner."""

    def __init__(self, base: float = 1.0, cap: float = 60.0, rng: random.Random | None = None) -> None:
        if base <= 0 or cap < base:
            raise ValueError("require 0 < base <= cap")
        self.base = base
        self.cap = cap
        self.attempt = 0
        self._rng = rng or random.Random()  # noqa: S311 — jitter, not crypto

    def next_delay(self) -> float:
        ceiling = min(self.cap, self.base * (2**self.attempt))
        self.attempt += 1
        return self._rng.uniform(ceiling / 2, ceiling)

    def reset(self) -> None:
        self.attempt = 0


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    description: str = "operation",
) -> T:
    """Retry an idempotent async operation. Never use for order placement."""
    backoff = ExponentialBackoff(base_delay, max_delay)
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except retry_on as exc:
            if attempt == attempts:
                raise
            delay = backoff.next_delay()
            logger.warning("%s failed (attempt %d/%d): %s — retrying in %.1fs",
                           description, attempt, attempts, exc, delay)
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


class SlidingWindowLimiter:
    """At most `max_events` per `window` seconds. Thread-safe; non-blocking check."""

    def __init__(self, max_events: int, window: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_events = max_events
        self.window = window
        self._clock = clock
        self._events: deque[float] = deque()
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            now = self._clock()
            while self._events and now - self._events[0] >= self.window:
                self._events.popleft()
            if len(self._events) >= self.max_events:
                return False
            self._events.append(now)
            return True


class AsyncRateLimiter:
    """Spaces calls at least `1/rate` seconds apart (broker API rate limits)."""

    def __init__(self, rate_per_second: float) -> None:
        self._interval = 1.0 / rate_per_second
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def __aenter__(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next = now + self._interval

    async def __aexit__(self, *exc: object) -> None:
        return None
