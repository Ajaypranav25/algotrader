"""Execution interface shared by paper, live and backtest brokers."""
from __future__ import annotations

from typing import Protocol

from algotrader.domain import OrderRequest, OrderResult


class Broker(Protocol):
    name: str

    async def place_order(self, req: OrderRequest) -> OrderResult:
        """
        Submit a market order and wait for its outcome.

        Must never raise for broker-side failures — return REJECTED when the order
        definitely did not execute and UNKNOWN when it may have.
        """
        ...
