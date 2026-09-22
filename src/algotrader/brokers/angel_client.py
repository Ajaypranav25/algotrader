"""
Thin async wrapper over the (synchronous, thread-unsafe) SmartAPI SDK.

* Every SDK call runs in a worker thread behind a lock + rate limiter, so the
  event loop never blocks and the shared requests.Session is never used
  concurrently.
* Read-only calls are retried and transparently re-authenticate on session
  errors. Order placement is NEVER retried — a retry after a timeout can
  double-fill.
* Credentials and tokens never reach the logs.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, TypeVar

import pyotp

from algotrader.config import Instrument, Settings
from algotrader.domain import Candle, OrderRequest
from algotrader.market_calendar import IST, Clock, SystemClock
from algotrader.resilience import AsyncRateLimiter, retry_async

logger = logging.getLogger(__name__)
T = TypeVar("T")

# SmartAPI session/auth error codes → re-login and retry once.
AUTH_ERROR_CODES = frozenset({"AG8001", "AG8002", "AG8003", "AB1010", "AB8050", "AB8051"})

INTERVAL_MINUTES = {
    "ONE_MINUTE": 1, "THREE_MINUTE": 3, "FIVE_MINUTE": 5, "TEN_MINUTE": 10,
    "FIFTEEN_MINUTE": 15, "THIRTY_MINUTE": 30, "ONE_HOUR": 60, "ONE_DAY": 375,
}

EXCHANGE_TYPE = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4, "MCX": 5, "NCDEX": 7, "CDS": 13}


class BrokerError(RuntimeError):
    pass


class AuthError(RuntimeError):
    """Login failed. Deliberately not a BrokerError: repeated bad logins can lock the account, so never retried."""


@dataclass(frozen=True, slots=True)
class FeedCredentials:
    jwt_token: str
    feed_token: str
    api_key: str
    client_code: str


@dataclass(frozen=True, slots=True)
class BrokerOrderState:
    order_id: str
    status: str            # lower-cased SmartAPI status: complete / rejected / cancelled / open / ...
    filled_quantity: int
    average_price: float
    message: str

    @property
    def is_terminal(self) -> bool:
        return self.status in {"complete", "rejected", "cancelled"}


def _is_ok(resp: Any) -> bool:
    return isinstance(resp, dict) and (resp.get("status") is True or str(resp.get("status")).lower() == "true")


def _error_code(resp: Any) -> str:
    if not isinstance(resp, dict):
        return ""
    return str(resp.get("errorcode") or resp.get("errorCode") or "")


def completed_candles(candles: list[Candle], interval: str, now: datetime) -> list[Candle]:
    """Drop the still-forming last bar so strategies never see a candle that will repaint."""
    minutes = INTERVAL_MINUTES.get(interval)
    if minutes is None or interval == "ONE_DAY":
        return candles
    width = timedelta(minutes=minutes)
    return [c for c in candles if c.timestamp + width <= now]


class AngelOneClient:
    def __init__(self, settings: Settings, clock: Clock | None = None,
                 connect_factory: Callable[[str], Any] | None = None) -> None:
        self._settings = settings
        self._clock = clock or SystemClock()
        self._connect_factory = connect_factory or _default_connect_factory
        self._smart: Any = None
        self._jwt: str | None = None
        self._refresh: str | None = None
        self._feed: str | None = None
        self._session_day: date | None = None
        self._sdk_lock = threading.Lock()           # SDK object is not thread-safe
        self._login_lock = asyncio.Lock()
        self._read_limiter = AsyncRateLimiter(3.0)  # historical API: ~3 req/s
        self._order_limiter = AsyncRateLimiter(8.0)

    # ── Session ──────────────────────────────────────────────────────────────
    @property
    def is_connected(self) -> bool:
        return self._smart is not None and self._jwt is not None

    def feed_credentials(self) -> FeedCredentials | None:
        if not (self._jwt and self._feed):
            return None
        return FeedCredentials(self._jwt, self._feed,
                               self._settings.angel_api_key.get_secret_value(),
                               self._settings.angel_client_id)

    async def login(self) -> None:
        async with self._login_lock:
            await asyncio.to_thread(self._login_sync)

    def _login_sync(self) -> None:
        s = self._settings
        if not s.has_broker_credentials:
            raise AuthError("Angel One credentials are not configured")
        with self._sdk_lock:
            smart = self._connect_factory(s.angel_api_key.get_secret_value())
            totp = pyotp.TOTP(s.angel_totp_secret.get_secret_value()).now()
            resp = smart.generateSession(s.angel_client_id, s.angel_password.get_secret_value(), totp)
            if not _is_ok(resp):
                self._smart = self._jwt = None
                message = resp.get("message", "") if isinstance(resp, dict) else ""
                raise AuthError(f"login rejected: {_error_code(resp) or '?'} {message}")
            data = resp.get("data") or {}
            self._jwt = data.get("jwtToken")
            self._refresh = data.get("refreshToken")
            self._feed = smart.getfeedToken()
            self._smart = smart
            self._session_day = self._clock.now().astimezone(IST).date()
        logger.info("Angel One login successful", extra={"ctx": {"client": s.angel_client_id}})

    async def ensure_session(self) -> None:
        """Log in if there is no session or it is from a previous trading day (sessions expire daily)."""
        today = self._clock.now().astimezone(IST).date()
        if not self.is_connected or self._session_day != today:
            await self.login()

    async def logout(self) -> None:
        if not self._smart:
            return
        def _logout() -> None:
            with self._sdk_lock:
                try:
                    self._smart.terminateSession(self._settings.angel_client_id)
                except Exception as exc:  # best effort on shutdown
                    logger.warning("logout failed: %s", exc)
        await asyncio.to_thread(_logout)
        self._smart = self._jwt = self._feed = None

    # ── Low-level call helpers ───────────────────────────────────────────────
    def _sdk(self, fn_name: str, *args: Any) -> Any:
        with self._sdk_lock:
            if self._smart is None:
                raise AuthError("not logged in")
            return getattr(self._smart, fn_name)(*args)

    async def _read(self, fn_name: str, *args: Any) -> Any:
        """Idempotent read: rate limited, retried, re-authenticates once on session errors."""
        async def attempt() -> Any:
            await self.ensure_session()
            async with self._read_limiter:
                resp = await asyncio.to_thread(self._sdk, fn_name, *args)
            if _error_code(resp) in AUTH_ERROR_CODES:
                logger.warning("session rejected (%s) during %s — re-authenticating", _error_code(resp), fn_name)
                await self.login()
                async with self._read_limiter:
                    resp = await asyncio.to_thread(self._sdk, fn_name, *args)
            if not _is_ok(resp):
                raise BrokerError(f"{fn_name} failed: {_error_code(resp)} "
                                  f"{resp.get('message', '') if isinstance(resp, dict) else resp!r}")
            return resp.get("data")

        return await retry_async(attempt, attempts=3, retry_on=(BrokerError, OSError, TimeoutError),
                                 description=f"SmartAPI {fn_name}")

    # ── Market data ──────────────────────────────────────────────────────────
    async def get_candles(self, instrument: Instrument, interval: str, lookback_days: int) -> list[Candle]:
        now = self._clock.now().astimezone(IST)
        params = {
            "exchange": instrument.exchange,
            "symboltoken": instrument.token,
            "interval": interval,
            "fromdate": (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d %H:%M"),
            "todate": now.strftime("%Y-%m-%d %H:%M"),
        }
        rows = await self._read("getCandleData", params) or []
        candles: list[Candle] = []
        for row in rows:
            try:
                ts = datetime.fromisoformat(str(row[0]))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=IST)
                candles.append(Candle(ts, float(row[1]), float(row[2]), float(row[3]),
                                      float(row[4]), int(row[5])))
            except (IndexError, ValueError, TypeError) as exc:
                logger.warning("skipping malformed candle for %s: %s (%s)", instrument.symbol, row, exc)
        candles.sort(key=lambda c: c.timestamp)
        return completed_candles(candles, interval, now)

    async def get_ltp(self, instrument: Instrument) -> float | None:
        data = await self._read("ltpData", instrument.exchange, instrument.broker_symbol, instrument.token)
        try:
            ltp = float((data or {})["ltp"])
        except (KeyError, TypeError, ValueError):
            return None
        return ltp if ltp > 0 else None

    async def get_positions(self) -> list[dict[str, Any]]:
        data = await self._read("position")
        return list(data or [])

    # ── Orders ───────────────────────────────────────────────────────────────
    async def place_market_order(self, req: OrderRequest) -> tuple[str | None, str | None, str]:
        """
        Submit an intraday market order exactly once.
        Returns (order_id, unique_order_id, message). order_id None means the broker
        explicitly rejected the request. Transport errors propagate to the caller,
        which must treat the outcome as unknown.
        """
        await self.ensure_session()
        params = {
            "variety": "NORMAL",
            "tradingsymbol": req.broker_symbol,
            "symboltoken": req.token,
            "transactiontype": req.side.value,
            "exchange": req.exchange,
            "ordertype": "MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": "0",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(req.quantity),
            "ordertag": req.tag[:20] if req.tag else None,
        }
        async with self._order_limiter:
            resp = await asyncio.to_thread(self._sdk, "placeOrderFullResponse", params)
        if _is_ok(resp):
            data = resp.get("data") or {}
            order_id = data.get("orderid")
            if order_id:
                return str(order_id), data.get("uniqueorderid"), str(resp.get("message", ""))
            # Accepted but no id: cannot be treated as rejected.
            raise BrokerError(f"order accepted without an order id: {resp!r}")
        message = resp.get("message", "rejected") if isinstance(resp, dict) else repr(resp)
        return None, None, f"{_error_code(resp)} {message}".strip()

    async def get_order_state(self, order_id: str, unique_order_id: str | None = None) -> BrokerOrderState | None:
        row: dict[str, Any] | None = None
        if unique_order_id:
            try:
                row = await self._read("individual_order_details", unique_order_id)
            except BrokerError as exc:
                logger.debug("individual_order_details failed, falling back to order book: %s", exc)
        if not row:
            book = await self._read("orderBook") or []
            row = next((r for r in book if str(r.get("orderid")) == order_id), None)
        if not row:
            return None
        return BrokerOrderState(
            order_id=order_id,
            status=str(row.get("status") or row.get("orderstatus") or "").lower(),
            filled_quantity=int(float(row.get("filledshares") or 0)),
            average_price=float(row.get("averageprice") or 0.0),
            message=str(row.get("text") or ""),
        )


def _default_connect_factory(api_key: str) -> Any:
    from SmartApi import SmartConnect  # imported lazily: heavy and only needed for live data

    return SmartConnect(api_key=api_key)
