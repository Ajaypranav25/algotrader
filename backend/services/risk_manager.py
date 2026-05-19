"""
services/risk_manager.py — Hardcoded trading guardrails.

Enforces:
  - Daily loss limit (auto-halt)
  - Max capital per trade
  - Max concurrent positions
  - Trailing stop-loss management
  - Market hours guard
"""
import pytz
from datetime import datetime
from typing import Dict, Optional, List

from core.config import get_settings
from models.schemas import GeminiSignal, WatchlistItem, PositionOut
from utils.logger import get_logger

settings = get_settings()
logger = get_logger("risk_manager")

IST = pytz.timezone("Asia/Kolkata")


class RiskManager:

    def __init__(self):
        self.daily_realized_pnl: float = 0.0       # Cumulative closed PnL today
        self.halt_triggered: bool = False            # True = bot stopped
        self.halt_reason: str = ""
        # Active positions: symbol -> PositionTracker
        self._positions: Dict[str, "_PositionTracker"] = {}

    # ── Market Hours ──────────────────────────────────────────────────────────

    def is_market_open(self) -> bool:
        """Returns True if current IST time is within NSE trading hours."""
        now = datetime.now(IST)

        # Skip weekends
        if now.weekday() >= 5:
            return False

        open_time = now.replace(
            hour=settings.market_open_hour,
            minute=settings.market_open_minute,
            second=0, microsecond=0,
        )
        close_time = now.replace(
            hour=settings.market_close_hour,
            minute=settings.market_close_minute,
            second=0, microsecond=0,
        )
        return open_time <= now <= close_time

    def minutes_to_close(self) -> int:
        """Minutes remaining until market close."""
        now = datetime.now(IST)
        close_time = now.replace(
            hour=settings.market_close_hour,
            minute=settings.market_close_minute,
            second=0, microsecond=0,
        )
        delta = (close_time - now).total_seconds()
        return max(0, int(delta / 60))

    # ── Pre-Trade Checks ──────────────────────────────────────────────────────

    def can_trade(self, symbol: str, signal: GeminiSignal, proposed_qty: int, price: float) -> tuple[bool, str]:
        """
        Master pre-trade validation. Returns (allowed, reason).
        ALL checks must pass before an order is routed.
        """
        # 1. Bot halt check
        if self.halt_triggered:
            return False, f"Bot halted: {self.halt_reason}"

        # 2. Market hours
        if not self.is_market_open():
            return False, "Market is closed"

        # 3. HOLD signal — never trade
        if signal.signal == "HOLD":
            return False, "Signal is HOLD"

        # 4. Daily loss limit
        if self.daily_realized_pnl <= -settings.max_daily_loss:
            self._trigger_halt(f"Daily loss limit hit: ₹{self.daily_realized_pnl:.2f}")
            return False, self.halt_reason

        # 5. Max open positions
        open_count = len(self._positions)
        if open_count >= settings.max_open_positions:
            return False, f"Max positions reached ({open_count}/{settings.max_open_positions})"

        # 6. Already holding this symbol
        if symbol in self._positions:
            return False, f"Already holding {symbol}"

        # 7. Capital per trade limit
        capital_required = proposed_qty * price
        if capital_required > settings.max_capital_per_trade:
            return False, (
                f"Capital required ₹{capital_required:.2f} exceeds limit "
                f"₹{settings.max_capital_per_trade:.2f}"
            )

        # 8. Confidence threshold (optional but recommended)
        if signal.confidence is not None and signal.confidence < 0.55:
            return False, f"Confidence too low: {signal.confidence:.0%} (min 55%)"

        # 9. SL/Target sanity check vs current price
        if signal.signal == "BUY":
            if signal.stop_loss and signal.stop_loss >= price:
                return False, f"BUY stop_loss ₹{signal.stop_loss} >= entry ₹{price}"
            if signal.target_price and signal.target_price <= price:
                return False, f"BUY target ₹{signal.target_price} <= entry ₹{price}"

        if signal.signal == "SELL":
            if signal.stop_loss and signal.stop_loss <= price:
                return False, f"SELL stop_loss ₹{signal.stop_loss} <= entry ₹{price}"
            if signal.target_price and signal.target_price >= price:
                return False, f"SELL target ₹{signal.target_price} >= entry ₹{price}"

        # 10. Don't trade in last 15 minutes (avoid MIS square-off chaos)
        if self.minutes_to_close() <= 15:
            return False, "No new trades within 15 min of market close"

        return True, "All checks passed"

    def calculate_quantity(self, price: float) -> int:
        """
        Derive quantity from max capital per trade.
        Always floor to whole shares; never buy 0.
        """
        qty = int(settings.max_capital_per_trade // price)
        return max(1, qty)

    # ── Position Tracking ─────────────────────────────────────────────────────

    def open_position(
        self,
        symbol: str,
        signal: GeminiSignal,
        entry_price: float,
        quantity: int,
    ):
        """Register a new open position for trailing SL tracking."""
        self._positions[symbol] = _PositionTracker(
            symbol=symbol,
            signal=signal.signal,
            entry_price=entry_price,
            quantity=quantity,
            stop_loss=signal.stop_loss,
            target_price=signal.target_price,
            trailing_pct=settings.trailing_stop_loss_pct,
        )
        logger.info(
            f"📌 Position opened: {signal.signal} {quantity}×{symbol} "
            f"@ ₹{entry_price} | SL: ₹{signal.stop_loss} | T: ₹{signal.target_price}"
        )

    def update_trailing_sl(self, symbol: str, current_price: float) -> Optional[str]:
        """
        Update trailing stop-loss for a position.
        Returns 'HIT_SL', 'HIT_TARGET', or None.
        """
        pos = self._positions.get(symbol)
        if not pos:
            return None
        return pos.update(current_price)

    def close_position(self, symbol: str, exit_price: float) -> float:
        """Mark position closed and return realized PnL."""
        pos = self._positions.pop(symbol, None)
        if not pos:
            return 0.0

        if pos.signal == "BUY":
            pnl = (exit_price - pos.entry_price) * pos.quantity
        else:
            pnl = (pos.entry_price - exit_price) * pos.quantity

        self.daily_realized_pnl += pnl
        logger.info(
            f"🔒 Position closed: {symbol} @ ₹{exit_price} | "
            f"PnL: ₹{pnl:+.2f} | Daily PnL: ₹{self.daily_realized_pnl:+.2f}"
        )

        # Check daily loss limit after close
        if self.daily_realized_pnl <= -settings.max_daily_loss:
            self._trigger_halt(f"Daily loss limit: ₹{self.daily_realized_pnl:.2f}")

        return pnl

    def get_positions_snapshot(self) -> List[PositionOut]:
        """Return current open positions for dashboard display."""
        result = []
        for pos in self._positions.values():
            ltp = pos.last_price or pos.entry_price
            if pos.signal == "BUY":
                upnl = (ltp - pos.entry_price) * pos.quantity
            else:
                upnl = (pos.entry_price - ltp) * pos.quantity
            pnl_pct = (upnl / (pos.entry_price * pos.quantity)) * 100 if pos.entry_price else 0

            result.append(PositionOut(
                symbol=pos.symbol,
                token="",
                quantity=pos.quantity,
                avg_price=pos.entry_price,
                ltp=ltp,
                unrealized_pnl=round(upnl, 2),
                pnl_pct=round(pnl_pct, 2),
                stop_loss=pos.current_sl,
                target_price=pos.target_price,
                trailing_sl=pos.current_sl,
                signal=pos.signal,
            ))
        return result

    def reset_daily(self):
        """Call at start of each trading day."""
        self.daily_realized_pnl = 0.0
        self.halt_triggered = False
        self.halt_reason = ""
        self._positions.clear()
        logger.info("🔄 Risk manager daily reset complete.")

    def _trigger_halt(self, reason: str):
        self.halt_triggered = True
        self.halt_reason = reason
        logger.critical(f"🛑 BOT HALTED: {reason}")

    def force_halt(self, reason: str = "Manual kill switch"):
        """External halt (from kill switch endpoint)."""
        self._trigger_halt(reason)

    def resume(self):
        """Resume after manual halt (admin action)."""
        if self.halt_triggered and "Daily loss" not in self.halt_reason:
            self.halt_triggered = False
            self.halt_reason = ""
            logger.info("▶️  Bot resumed by admin.")
        else:
            logger.warning("Cannot auto-resume — daily loss limit active. Reset tomorrow.")


# ── Position Tracker (internal) ───────────────────────────────────────────────

class _PositionTracker:
    def __init__(
        self,
        symbol: str,
        signal: str,
        entry_price: float,
        quantity: int,
        stop_loss: Optional[float],
        target_price: Optional[float],
        trailing_pct: float,
    ):
        self.symbol = symbol
        self.signal = signal
        self.entry_price = entry_price
        self.quantity = quantity
        self.initial_sl = stop_loss or 0.0
        self.current_sl = stop_loss or 0.0
        self.target_price = target_price or 0.0
        self.trailing_pct = trailing_pct / 100.0   # Convert % to decimal
        self.last_price: Optional[float] = None
        self.peak_price: float = entry_price        # Highest (BUY) / Lowest (SELL) seen

    def update(self, price: float) -> Optional[str]:
        """
        Advance trailing SL and check exit conditions.
        Returns: 'HIT_SL' | 'HIT_TARGET' | None
        """
        self.last_price = price

        if self.signal == "BUY":
            # Advance peak
            if price > self.peak_price:
                self.peak_price = price
                new_sl = price * (1 - self.trailing_pct)
                if new_sl > self.current_sl:    # Only move SL up (never down)
                    self.current_sl = round(new_sl, 2)
                    logger.debug(f"🔼 Trailing SL for {self.symbol} → ₹{self.current_sl}")

            # Exit checks
            if price <= self.current_sl:
                logger.warning(f"⛔ SL HIT: {self.symbol} @ ₹{price} (SL: ₹{self.current_sl})")
                return "HIT_SL"
            if self.target_price and price >= self.target_price:
                logger.info(f"🎯 TARGET HIT: {self.symbol} @ ₹{price}")
                return "HIT_TARGET"

        elif self.signal == "SELL":
            # Advance peak (downward for shorts)
            if price < self.peak_price:
                self.peak_price = price
                new_sl = price * (1 + self.trailing_pct)
                if new_sl < self.current_sl:    # Only move SL down (never up)
                    self.current_sl = round(new_sl, 2)
                    logger.debug(f"🔽 Trailing SL for {self.symbol} → ₹{self.current_sl}")

            # Exit checks
            if price >= self.current_sl:
                logger.warning(f"⛔ SL HIT (SHORT): {self.symbol} @ ₹{price}")
                return "HIT_SL"
            if self.target_price and price <= self.target_price:
                logger.info(f"🎯 TARGET HIT (SHORT): {self.symbol} @ ₹{price}")
                return "HIT_TARGET"

        return None


# ── Singleton ─────────────────────────────────────────────────────────────────
risk_manager = RiskManager()
