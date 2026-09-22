"""
Live execution via Angel One.

An order is only reported FILLED once the broker confirms it `complete`. If we
cannot get a definitive answer before the timeout the result is UNKNOWN, and
the engine halts new entries rather than guess.
"""
from __future__ import annotations

import asyncio
import logging
import time

from algotrader.brokers.angel_client import AngelOneClient
from algotrader.domain import OrderRequest, OrderResult, OrderStatus

logger = logging.getLogger(__name__)


class LiveBroker:
    name = "angel_one"

    def __init__(self, client: AngelOneClient, fill_timeout: float = 15.0, poll_interval: float = 0.5,
                 fee_per_order: float = 20.0) -> None:
        self._client = client
        self._timeout = fill_timeout
        self._poll = poll_interval
        # Estimated charges so the daily-loss limit is conservative; the broker
        # contract note remains the source of truth.
        self._fee = fee_per_order

    async def place_order(self, req: OrderRequest) -> OrderResult:
        try:
            order_id, unique_id, message = await self._client.place_market_order(req)
        except Exception as exc:
            logger.critical("order submission outcome unknown", extra={"ctx": {
                "symbol": req.symbol, "side": req.side.value, "qty": req.quantity, "error": str(exc)}})
            return OrderResult(OrderStatus.UNKNOWN, None, message=f"submission error: {exc}")

        if order_id is None:
            logger.error("order rejected", extra={"ctx": {"symbol": req.symbol, "reason": message}})
            return OrderResult(OrderStatus.REJECTED, None, message=message)

        logger.info("order accepted", extra={"ctx": {
            "symbol": req.symbol, "side": req.side.value, "qty": req.quantity, "order_id": order_id}})
        return await self._await_fill(req, order_id, unique_id)

    async def _await_fill(self, req: OrderRequest, order_id: str, unique_id: str | None) -> OrderResult:
        deadline = time.monotonic() + self._timeout
        last_status = "unknown"
        while time.monotonic() < deadline:
            try:
                state = await self._client.get_order_state(order_id, unique_id)
            except Exception as exc:  # polling must never abort the wait
                logger.warning("order status poll failed for %s: %s", order_id, exc)
                state = None
            if state is not None:
                last_status = state.status
                if state.status == "complete":
                    return OrderResult(OrderStatus.FILLED, order_id, state.filled_quantity or req.quantity,
                                       state.average_price, self._fee, "complete")
                if state.status in {"rejected", "cancelled"}:
                    if state.filled_quantity > 0:
                        # Partially filled then cancelled — we own shares.
                        return OrderResult(OrderStatus.FILLED, order_id, state.filled_quantity,
                                           state.average_price, self._fee, f"partial fill then {state.status}")
                    return OrderResult(OrderStatus.REJECTED, order_id, message=state.message or state.status)
            await asyncio.sleep(self._poll)

        logger.critical("order fill not confirmed before timeout", extra={"ctx": {
            "symbol": req.symbol, "order_id": order_id, "last_status": last_status}})
        return OrderResult(OrderStatus.UNKNOWN, order_id, message=f"unconfirmed after {self._timeout}s ({last_status})")
