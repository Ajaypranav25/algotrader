"""
In-process pub/sub. Each subscriber gets its own bounded queue, so every
dashboard client sees every event, and a slow client drops its own oldest
events without blocking the trading engine.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any


class EventBus:
    def __init__(self, maxsize: int = 500) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._maxsize = maxsize

    def publish(self, event: str, data: dict[str, Any]) -> None:
        message = {"event": event, "data": data, "ts": datetime.now(UTC).isoformat()}
        for q in list(self._subscribers):
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(message)

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(self._maxsize)
        self._subscribers.add(q)
        try:
            yield q
        finally:
            self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
