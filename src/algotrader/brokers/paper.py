"""
Simulated execution for paper trading and backtesting.

Fills market orders immediately at the current price adjusted by a fixed
slippage (deterministic, so backtests are reproducible) and charges a flat
per-order fee.
"""
from __future__ import annotations

import itertools
import logging
from collections.abc import Callable

from algotrader.domain import OrderRequest, OrderResult, OrderStatus, Side

logger = logging.getLogger(__name__)

PriceProvider = Callable[[str], float | None]


class PaperBroker:
    name = "paper"

    def __init__(self, price_provider: PriceProvider, slippage_bps: float = 2.0, fee_per_order: float = 20.0) -> None:
        self._price = price_provider
        self.slippage = slippage_bps / 10_000
        self.fee_per_order = fee_per_order
        self._ids = itertools.count(1)
        self.fills: list[tuple[OrderRequest, OrderResult]] = []

    def fill_price(self, side: Side, market: float) -> float:
        adj = 1 + self.slippage if side is Side.BUY else 1 - self.slippage
        return round(market * adj, 2)

    async def place_order(self, req: OrderRequest) -> OrderResult:
        market = self._price(req.symbol)
        if market is None or market <= 0:
            return OrderResult(OrderStatus.REJECTED, None, message=f"no market price for {req.symbol}")
        result = OrderResult(
            status=OrderStatus.FILLED,
            order_id=f"PAPER-{next(self._ids):06d}",
            filled_quantity=req.quantity,
            average_price=self.fill_price(req.side, market),
            fees=self.fee_per_order,
            message="simulated fill",
        )
        self.fills.append((req, result))
        logger.info("paper fill", extra={"ctx": {
            "symbol": req.symbol, "side": req.side.value, "qty": req.quantity,
            "price": result.average_price, "order_id": result.order_id}})
        return result
