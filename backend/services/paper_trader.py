"""
services/paper_trader.py — Simulated trade execution engine.

When PAPER_TRADING=true, this module handles ALL order simulation:
  - Realistic fill prices (adds slippage simulation)
  - Simulated order book with order IDs
  - In-memory position tracking separate from live
  - Simulated PnL calculation
  - Full audit trail written to DB with is_paper=True flag

This lets you run the ENTIRE system end-to-end without touching real capital.
"""
import uuid
import random
from datetime import datetime
from typing import Dict, List, Optional, Any

from models.schemas import OrderRequest, OrderResponse, PositionOut, WatchlistItem
from utils.logger import get_logger

logger = get_logger("paper_trader")

# Slippage simulation: random between 0.01% and 0.05% of price
SLIPPAGE_MIN_PCT = 0.0001
SLIPPAGE_MAX_PCT = 0.0005


class PaperOrder:
    """Represents a simulated order."""
    def __init__(
        self,
        order_id: str,
        symbol: str,
        token: str,
        side: str,
        quantity: int,
        fill_price: float,
        order_type: str,
        timestamp: datetime,
    ):
        self.order_id = order_id
        self.symbol = symbol
        self.token = token
        self.side = side                    # BUY / SELL
        self.quantity = quantity
        self.fill_price = fill_price
        self.order_type = order_type
        self.timestamp = timestamp
        self.status = "FILLED"             # Paper orders fill instantly


class PaperPosition:
    """Represents a simulated open position."""
    def __init__(self, symbol: str, token: str, side: str, quantity: int, avg_price: float):
        self.symbol = symbol
        self.token = token
        self.side = side
        self.quantity = quantity
        self.avg_price = avg_price
        self.current_price: float = avg_price
        self.opened_at: datetime = datetime.now()
        self.stop_loss: float = 0.0
        self.target_price: float = 0.0
        self.trailing_sl: float = 0.0

    @property
    def unrealized_pnl(self) -> float:
        if self.side == "BUY":
            return (self.current_price - self.avg_price) * self.quantity
        else:
            return (self.avg_price - self.current_price) * self.quantity

    @property
    def pnl_pct(self) -> float:
        if self.avg_price == 0:
            return 0.0
        return (self.unrealized_pnl / (self.avg_price * self.quantity)) * 100


class PaperTrader:
    """
    Full paper trading engine.

    Maintains its own positions, order log, and PnL separate from live trading.
    Designed to be a drop-in replacement for real Angel One order calls.
    """

    def __init__(self):
        self.positions: Dict[str, PaperPosition] = {}      # symbol → PaperPosition
        self.orders: List[PaperOrder] = []                  # Complete order history
        self.realized_pnl: float = 0.0
        self.total_trades: int = 0
        self.winning_trades: int = 0
        self._order_counter: int = 0

    # ── Order Execution ───────────────────────────────────────────────────────

    def place_order(self, req: OrderRequest, market_price: Optional[float] = None) -> OrderResponse:
        """
        Simulate order placement with realistic fill price.
        Returns an OrderResponse identical in structure to the live version.
        """
        fill_price = self._simulate_fill_price(
            req.price if req.price and req.price > 0 else (market_price or req.price),
            req.transaction_type,
        )

        order_id = self._generate_order_id()
        order = PaperOrder(
            order_id=order_id,
            symbol=req.symbol,
            token=req.token,
            side=req.transaction_type,
            quantity=req.quantity,
            fill_price=fill_price,
            order_type=req.order_type,
            timestamp=datetime.now(),
        )
        self.orders.append(order)

        # Update positions
        if req.transaction_type == "BUY":
            self._open_or_add(req.symbol, req.token, "BUY", req.quantity, fill_price)
        elif req.transaction_type == "SELL":
            self._close_or_short(req.symbol, req.token, "SELL", req.quantity, fill_price)

        self.total_trades += 1

        logger.info(
            f"[PAPER] {req.transaction_type} {req.quantity}×{req.symbol} "
            f"@ ₹{fill_price:.2f} (requested: ₹{req.price or 'MARKET'}) | "
            f"OrderID: {order_id}"
        )

        return OrderResponse(
            success=True,
            order_id=order_id,
            message=f"Paper order filled @ ₹{fill_price:.2f} (slippage simulated)",
            is_paper=True,
        )

    def _simulate_fill_price(self, base_price: float, side: str) -> float:
        """
        Add realistic slippage to simulate market impact.
        BUY fills slightly above, SELL fills slightly below.
        """
        if not base_price or base_price <= 0:
            return base_price or 0.0

        slippage_pct = random.uniform(SLIPPAGE_MIN_PCT, SLIPPAGE_MAX_PCT)
        slippage_amt = base_price * slippage_pct

        if side == "BUY":
            return round(base_price + slippage_amt, 2)
        else:
            return round(base_price - slippage_amt, 2)

    def _open_or_add(self, symbol: str, token: str, side: str, qty: int, price: float):
        """Open a new paper position or add to an existing one (average up/down)."""
        if symbol in self.positions:
            pos = self.positions[symbol]
            if pos.side == side:
                # Average into position
                total_qty = pos.quantity + qty
                pos.avg_price = (pos.avg_price * pos.quantity + price * qty) / total_qty
                pos.quantity = total_qty
            else:
                # Opposite side — partial or full close
                self._close_or_short(symbol, token, side, qty, price)
        else:
            self.positions[symbol] = PaperPosition(symbol, token, side, qty, price)

    def _close_or_short(self, symbol: str, token: str, side: str, qty: int, price: float):
        """Close an existing position and calculate realized PnL."""
        if symbol not in self.positions:
            # Opening a short position (if supported)
            self.positions[symbol] = PaperPosition(symbol, token, "SELL", qty, price)
            return

        pos = self.positions[symbol]
        close_qty = min(qty, pos.quantity)

        if pos.side == "BUY":
            trade_pnl = (price - pos.avg_price) * close_qty
        else:
            trade_pnl = (pos.avg_price - price) * close_qty

        self.realized_pnl += trade_pnl
        if trade_pnl > 0:
            self.winning_trades += 1

        logger.info(
            f"[PAPER] Position closed: {symbol} | PnL: ₹{trade_pnl:+.2f} | "
            f"Running PnL: ₹{self.realized_pnl:+.2f}"
        )

        pos.quantity -= close_qty
        if pos.quantity <= 0:
            del self.positions[symbol]

    def update_prices(self, ticks: Dict[str, float]):
        """Update current prices for all open paper positions."""
        for symbol, pos in self.positions.items():
            if symbol in ticks:
                pos.current_price = ticks[symbol]

    def set_position_levels(self, symbol: str, stop_loss: float, target: float, trailing_sl: float):
        """Set SL/Target on a paper position (called after signal parsing)."""
        if symbol in self.positions:
            pos = self.positions[symbol]
            pos.stop_loss = stop_loss
            pos.target_price = target
            pos.trailing_sl = trailing_sl

    # ── Portfolio / Reporting ─────────────────────────────────────────────────

    def get_positions(self) -> List[PositionOut]:
        """Return open paper positions in dashboard format."""
        result = []
        for pos in self.positions.values():
            result.append(PositionOut(
                symbol=pos.symbol,
                token=pos.token,
                quantity=pos.quantity,
                avg_price=pos.avg_price,
                ltp=pos.current_price,
                unrealized_pnl=round(pos.unrealized_pnl, 2),
                pnl_pct=round(pos.pnl_pct, 2),
                stop_loss=pos.stop_loss,
                target_price=pos.target_price,
                trailing_sl=pos.trailing_sl,
            ))
        return result

    def get_total_unrealized_pnl(self) -> float:
        return round(sum(p.unrealized_pnl for p in self.positions.values()), 2)

    def get_summary(self) -> Dict[str, Any]:
        """Return a summary dict for the dashboard."""
        win_rate = (
            round((self.winning_trades / self.total_trades) * 100, 1)
            if self.total_trades > 0 else 0.0
        )
        return {
            "mode": "PAPER",
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": self.get_total_unrealized_pnl(),
            "total_pnl": round(self.realized_pnl + self.get_total_unrealized_pnl(), 2),
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "win_rate_pct": win_rate,
            "open_positions": len(self.positions),
            "total_orders": len(self.orders),
        }

    def get_order_history(self) -> List[Dict[str, Any]]:
        """Return all simulated orders as dicts."""
        return [
            {
                "order_id": o.order_id,
                "symbol": o.symbol,
                "side": o.side,
                "quantity": o.quantity,
                "fill_price": o.fill_price,
                "order_type": o.order_type,
                "status": o.status,
                "timestamp": o.timestamp.isoformat(),
            }
            for o in self.orders
        ]

    def reset(self):
        """Reset paper trading state (e.g., start of new trading day)."""
        self.positions.clear()
        self.orders.clear()
        self.realized_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self._order_counter = 0
        logger.info("📄 Paper trader state reset for new trading day.")

    def cancel_all_positions(self, current_prices: Dict[str, float]) -> Dict[str, Any]:
        """
        Simulate kill switch — close all paper positions at current market price.
        """
        logger.warning("[PAPER] Kill switch — closing all paper positions")
        closed = 0

        for symbol in list(self.positions.keys()):
            pos = self.positions[symbol]
            exit_price = current_prices.get(symbol, pos.current_price)
            exit_side = "SELL" if pos.side == "BUY" else "BUY"

            req = OrderRequest(
                symbol=symbol,
                token=pos.token,
                exchange="NSE",
                transaction_type=exit_side,
                quantity=pos.quantity,
                order_type="MARKET",
            )
            self.place_order(req, exit_price)
            closed += 1

        return {"success": True, "closed": closed, "is_paper": True}

    # ── Internal ──────────────────────────────────────────────────────────────

    def _generate_order_id(self) -> str:
        """Generate a realistic-looking paper order ID."""
        self._order_counter += 1
        ts = datetime.now().strftime("%H%M%S")
        return f"PAPER-{ts}-{self._order_counter:04d}-{uuid.uuid4().hex[:4].upper()}"


# ── Singleton ─────────────────────────────────────────────────────────────────
paper_trader = PaperTrader()