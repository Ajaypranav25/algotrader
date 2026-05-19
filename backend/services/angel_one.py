"""
services/angel_one.py — Angel One SmartAPI integration.

Handles:
  - Automated daily login with TOTP (no manual 2FA)
  - Historical candle data fetching
  - Live WebSocket tick data with non-blocking deferred subscription handling
  - Order placement (wrapped with paper-trading guard)
  - Portfolio / position fetching
  - Token verification against live scrip master
"""
import json
import asyncio
import threading
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import httpx
import pyotp
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

from core.config import get_settings
from models.schemas import Candle, WatchlistItem, OrderRequest, OrderResponse
from utils.logger import get_logger

settings = get_settings()
logger = get_logger("angel_one")


class AngelOneService:
    """
    Encapsulates all Angel One SmartAPI operations.
    Thread-safe singleton designed for use from FastAPI async context.
    """

    def __init__(self):
        self.smart: Optional[SmartConnect] = None
        self.jwt_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.feed_token: Optional[str] = None
        self.is_connected: bool = False
        self._last_login: Optional[datetime] = None
        self._tick_data: Dict[str, float] = {}     # token -> LTP
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_active: bool = False

    # ── Authentication ────────────────────────────────────────────────────────

    async def login(self) -> bool:
        """
        Perform automated login using stored credentials + TOTP.
        Angel One sessions typically last one trading day.
        Returns True on success.
        """
        try:
            logger.info("Initiating Angel One SmartAPI login...")

            totp_code = pyotp.TOTP(settings.angel_totp_secret).now()
            logger.debug(f"Generated TOTP: {totp_code}")

            self.smart = SmartConnect(api_key=settings.angel_api_key)

            data = self.smart.generateSession(
                clientCode=settings.angel_client_id,
                password=settings.angel_password,
                totp=totp_code,
            )

            if data.get("status") is False:
                logger.error(f"Login failed: {data.get('message', 'Unknown error')}")
                self.is_connected = False
                return False

            self.jwt_token    = data["data"]["jwtToken"]
            self.refresh_token = data["data"]["refreshToken"]
            self.feed_token   = self.smart.getfeedToken()

            self.smart.setAccessToken(self.jwt_token)
            self.is_connected = True
            self._last_login  = datetime.now()

            logger.info(
                f"✅ Angel One login successful | Client: {settings.angel_client_id} | "
                f"Mode: {settings.mode_label}"
            )
            return True

        except Exception as e:
            logger.error(f"Angel One login exception: {e}", exc_info=True)
            self.is_connected = False
            return False

    async def refresh_session(self) -> bool:
        """Refresh JWT using the refresh token to extend session."""
        try:
            data = self.smart.generateToken(self.refresh_token)
            if data.get("status"):
                self.jwt_token = data["data"]["jwtToken"]
                self.smart.setAccessToken(self.jwt_token)
                logger.info("Session token refreshed successfully.")
                return True
        except Exception as e:
            logger.warning(f"Session refresh failed, will re-login: {e}")
        return await self.login()

    def ensure_connected(self) -> bool:
        """Guard: raise if not connected."""
        if not self.is_connected or not self.smart:
            raise RuntimeError("Angel One SmartAPI is not connected. Call login() first.")
        return True

    # ── Market Data ───────────────────────────────────────────────────────────

    async def get_candles(
        self,
        token: str,
        symbol: str,
        exchange: str = "NSE",
        interval: str = "FIFTEEN_MINUTE",
        lookback_days: int = 1,
    ) -> List[Candle]:
        """
        Fetch OHLCV candle data for a given symbol.

        Angel One API response structure:
          Success → {"status": True,  "message": "SUCCESS", "data": [[ts,o,h,l,c,v], ...]}
          Error   → {"status": False, "message": "...",     "errorcode": "AG8001"}

        Intervals: ONE_MINUTE, THREE_MINUTE, FIVE_MINUTE, TEN_MINUTE,
                   FIFTEEN_MINUTE, THIRTY_MINUTE, ONE_HOUR, ONE_DAY
        """
        self.ensure_connected()

        to_date   = datetime.now()
        from_date = to_date - timedelta(days=lookback_days)

        params = {
            "exchange":    exchange,
            "symboltoken": token,
            # API requires exactly "yyyy-MM-dd HH:mm" — 24-hour clock
            "fromdate":    from_date.strftime("%Y-%m-%d %H:%M"),
            "todate":      to_date.strftime("%Y-%m-%d %H:%M"),
            "interval":    interval,
        }

        logger.debug(f"getCandleData params for {symbol}: {params}")

        try:
            resp = await asyncio.to_thread(self.smart.getCandleData, params)

            logger.debug(f"getCandleData raw response for {symbol}: {resp}")

            # The SmartAPI Python SDK normalises both success and error
            # responses to use "status" (bool). Guard against string "true"
            # returned by some older SDK builds as well.
            status_ok = (
                resp.get("status") is True
                or str(resp.get("status", "")).lower() == "true"
                or resp.get("success") is True          # fallback for older SDK
            )

            if not status_ok:
                error_code = (
                    resp.get("errorcode")
                    or resp.get("errorCode")
                    or "unknown"
                )
                message = resp.get("message", "No message returned")

                if error_code in ("AG8001", "AB8050"):
                    logger.error(
                        f"[{symbol}] Invalid token ({error_code}): {message}\n"
                        f"  → Token used     : {token!r}\n"
                        f"  → Likely fix 1   : Enable 'Historical Data' in your Angel One app\n"
                        f"                     https://smartapi.angelbroking.com\n"
                        f"  → Likely fix 2   : Token is stale — run verify_tokens() to check\n"
                        f"                     against the live scrip master."
                    )
                elif error_code in ("AB1004", "AB1005"):
                    logger.error(
                        f"[{symbol}] Session expired ({error_code}). "
                        "Calling refresh_session()..."
                    )
                    await self.refresh_session()
                else:
                    logger.warning(
                        f"getCandleData failed for {symbol}: "
                        f"code={error_code!r}  message={message!r}"
                    )
                return []

            raw_data = resp.get("data") or []
            if not raw_data:
                logger.warning(
                    f"No candle records returned for {symbol} (empty data array). "
                    "Market may be closed or the date range has no trading data."
                )
                return []

            candles: List[Candle] = []
            for row in raw_data:
                # row = [timestamp, open, high, low, close, volume]
                try:
                    candles.append(Candle(
                        timestamp = str(row[0]),
                        open      = float(row[1]),
                        high      = float(row[2]),
                        low       = float(row[3]),
                        close     = float(row[4]),
                        volume    = int(row[5]),
                    ))
                except (IndexError, ValueError, TypeError) as e:
                    logger.warning(
                        f"Skipping malformed candle row for {symbol}: {row} — {e}"
                    )

            logger.debug(f"Fetched {len(candles)} candles for {symbol} ({interval})")
            return candles

        except Exception as e:
            logger.error(f"Exception in get_candles for {symbol}: {e}", exc_info=True)
            return []

    async def get_ltp(self, exchange: str, symbol: str, token: str) -> Optional[float]:
        """Fetch Last Traded Price for a single instrument."""
        self.ensure_connected()
        try:
            resp = await asyncio.to_thread(
                self.smart.ltpData, exchange, symbol, token
            )
            if resp.get("status"):
                return float(resp["data"]["ltp"])
        except Exception as e:
            logger.error(f"LTP fetch failed for {symbol}: {e}")
        return None

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Fetch all open intraday positions."""
        self.ensure_connected()
        try:
            resp = await asyncio.to_thread(self.smart.position)
            if resp.get("status") and resp.get("data"):
                return resp["data"]
        except Exception as e:
            logger.error(f"Position fetch failed: {e}")
        return []

    async def get_portfolio_value(self) -> float:
        """Return total portfolio value (funds available)."""
        self.ensure_connected()
        try:
            resp = await asyncio.to_thread(self.smart.rmsLimit)
            if resp.get("status"):
                return float(resp["data"].get("net", 0))
        except Exception as e:
            logger.error(f"RMS limit fetch failed: {e}")
        return 0.0

    # ── Token Verification ────────────────────────────────────────────────────

    async def verify_tokens(self, watchlist: Optional[List[WatchlistItem]] = None) -> None:
        """
        Downloads the live Angel One scrip master and verifies every token
        in the supplied watchlist (or config/watchlist.json).

        Run once from the command line to diagnose AG8001 errors:

            cd backend
            python -c "
            import asyncio
            from services.angel_one import angel_service
            asyncio.run(angel_service.verify_tokens())
            "
        """
        import json
        from pathlib import Path

        SCRIP_MASTER_URL = (
            "https://margincalculator.angelbroking.com"
            "/OpenAPI_File/files/OpenAPIScripMaster.json"
        )

        # Fall back to watchlist.json if nothing is passed in
        if not watchlist:
            config_path = Path("../config/watchlist.json")
            if config_path.exists():
                with open(config_path) as f:
                    raw = json.load(f)
                watchlist = [WatchlistItem(**item) for item in raw]
            else:
                logger.error("verify_tokens: no watchlist supplied and watchlist.json not found.")
                return

        logger.info("Downloading live scrip master from Angel One…")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(SCRIP_MASTER_URL)
                r.raise_for_status()
                master: list = r.json()
        except Exception as e:
            logger.error(f"Failed to download scrip master: {e}")
            return

        # Build lookup: token -> record
        token_map: Dict[str, dict] = {
            str(entry["token"]): entry for entry in master
        }

        print("\n" + "=" * 65)
        print("  ANGEL ONE TOKEN VERIFICATION REPORT")
        print("=" * 65)

        all_ok = True
        for item in watchlist:
            record = token_map.get(str(item.token))
            if not record:
                print(
                    f"  ❌  {item.symbol:12} token={item.token!r:8} "
                    f"NOT FOUND in scrip master"
                )
                all_ok = False
                continue

            master_sym  = record.get("symbol",   "?")
            master_exch = record.get("exch_seg", "?")
            match = (
                master_sym.upper().startswith(item.symbol.upper())
                and master_exch.upper() == item.exchange.upper()
            )
            status = "✅" if match else "⚠️  MISMATCH"
            print(
                f"  {status}  {item.symbol:12} token={item.token!r:8} "
                f"→ master=({master_sym!r}, {master_exch!r})"
            )
            if not match:
                all_ok = False

        print("=" * 65)
        if all_ok:
            print("  All tokens verified ✅")
        else:
            print("  ⚠️  Fix mismatched tokens in config/watchlist.json")
            print("  Search the scrip master for your symbol:")
            print('  python -c "')
            print('  import httpx, asyncio, json')
            print('  m = asyncio.run(httpx.AsyncClient().get(')
            print('      \"https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json\"))')
            print('  hits = [x for x in m.json() if \"RELIANCE\" in x.get(\"symbol\",\"\") and x.get(\"exch_seg\")==\"NSE\"]')
            print('  print(json.dumps(hits[:5], indent=2))')
            print('  "')
        print()

    # ── WebSocket (Live Ticks) ────────────────────────────────────────────────

    def start_websocket(self, watchlist: List[WatchlistItem]):
        """
        Start a background thread streaming live tick data via SmartWebSocketV2.
        Updates self._tick_data[token] with latest LTP.
        """
        if self._ws_active:
            logger.info("WebSocket already running.")
            return

        token_list = [
            {"exchangeType": 1, "tokens": [item.token]}   # 1 = NSE
            for item in watchlist
        ]

        correlation_id = "algotrader_stream"

        # Container so nested callbacks can reference the instance
        sws_container: Dict[str, Any] = {"instance": None}

        def on_data(wsapp, message):
            try:
                if isinstance(message, bytes):
                    import struct
                    tok = str(int.from_bytes(message[27:31], "little"))
                    ltp = struct.unpack("<f", message[43:47])[0]
                    self._tick_data[tok] = round(ltp, 2)
            except Exception:
                pass

        def on_error(wsapp, error):
            logger.error(f"WebSocket error: {error}")
            self._ws_active = False

        def on_close(wsapp):
            logger.warning("WebSocket connection closed.")
            self._ws_active = False

        def on_open(wsapp):
            logger.info("✅ WebSocket connection opened — authenticating channel...")
            self._ws_active = True

            # Defer subscription by 1 s so the handshake completes first
            def delayed_subscribe():
                time.sleep(1)
                logger.info(
                    f"Sending subscription payload for {len(token_list)} tokens..."
                )
                try:
                    sws = sws_container["instance"]
                    if sws is None:
                        logger.error(
                            "Subscription aborted: SmartWebSocketV2 instance not ready."
                        )
                        return
                    sws.subscribe(correlation_id, mode=1, token_list=token_list)
                    logger.info("✅ Watchlist tokens subscribed successfully.")
                except Exception as e:
                    logger.error(f"Subscription failed: {e}")

            threading.Thread(target=delayed_subscribe, daemon=True).start()

        def _run():
            try:
                from services.risk_manager import risk_manager  # local import avoids circular

                sws = SmartWebSocketV2(
                    auth_token  = self.jwt_token,
                    api_key     = settings.angel_api_key,
                    client_code = settings.angel_client_id,
                    feed_token  = self.feed_token,
                )
                sws_container["instance"] = sws

                sws.on_open  = on_open
                sws.on_data  = on_data
                sws.on_error = on_error
                sws.on_close = on_close

                if risk_manager.is_market_open():
                    logger.info(
                        "Market is open. Initializing live tick WebSocket stream..."
                    )
                    sws.connect()
                else:
                    logger.warning(
                        "⚠️  Market is closed. Skipping live WebSocket stream setup."
                    )

            except Exception as e:
                logger.error(f"WebSocket thread crashed: {e}", exc_info=True)
                self._ws_active = False

        self._ws_thread = threading.Thread(
            target=_run, daemon=True, name="WS-Stream"
        )
        self._ws_thread.start()
        logger.info("WebSocket thread started.")

    def get_live_ltp(self, token: str) -> Optional[float]:
        """Get cached live price from WebSocket stream."""
        return self._tick_data.get(token)

    # ── Order Execution ───────────────────────────────────────────────────────

    async def place_order(self, req: OrderRequest) -> OrderResponse:
        """
        Place an order on Angel One.
        In paper-trading mode this is a no-op that returns a mock response.
        """
        if settings.paper_trading:
            logger.info(
                f"[PAPER] {req.transaction_type} {req.quantity} × {req.symbol} "
                f"@ ₹{req.price or 'MARKET'}"
            )
            return OrderResponse(
                success  = True,
                order_id = f"PAPER-{datetime.now().strftime('%H%M%S%f')[:12]}",
                message  = "Paper trade executed (no real order placed)",
                is_paper = True,
            )

        # ── Live order ───────────────────────────────────────────────────────
        self.ensure_connected()
        order_params = {
            "variety":         "NORMAL",
            "tradingsymbol":   req.symbol,
            "symboltoken":     req.token,
            "transactiontype": req.transaction_type,
            "exchange":        req.exchange,
            "ordertype":       req.order_type,
            "producttype":     req.product_type,
            "duration":        "DAY",
            "price":           str(req.price) if req.order_type == "LIMIT" else "0",
            "squareoff":       "0",
            "stoploss":        "0",
            "quantity":        str(req.quantity),
        }

        try:
            resp = await asyncio.to_thread(self.smart.placeOrder, order_params)
            if resp.get("status"):
                order_id = resp["data"]["orderid"]
                logger.info(
                    f"✅ LIVE ORDER | {req.transaction_type} {req.quantity} × "
                    f"{req.symbol} | OrderID: {order_id}"
                )
                return OrderResponse(
                    success  = True,
                    order_id = order_id,
                    message  = f"Order placed successfully: {order_id}",
                    is_paper = False,
                )
            else:
                msg = resp.get("message", "Unknown error")
                logger.error(f"Order failed: {msg}")
                return OrderResponse(
                    success=False, order_id=None, message=msg, is_paper=False
                )

        except Exception as e:
            logger.error(f"Order exception for {req.symbol}: {e}", exc_info=True)
            return OrderResponse(
                success=False, order_id=None, message=str(e), is_paper=False
            )

    async def cancel_all_positions(self) -> Dict[str, Any]:
        """
        KILL SWITCH — Square off all open intraday positions.
        Places SELL orders for all BUY positions and vice-versa.
        """
        logger.warning("🚨 KILL SWITCH ACTIVATED — Squaring off all positions")

        if settings.paper_trading:
            logger.info("[PAPER] Kill switch simulated — no real orders.")
            return {"success": True, "closed": 0, "is_paper": True}

        positions = await self.get_positions()
        closed_count = 0

        for pos in positions:
            qty = int(pos.get("netqty", 0))
            if qty == 0:
                continue

            side = "SELL" if qty > 0 else "BUY"
            req = OrderRequest(
                symbol           = pos["tradingsymbol"],
                token            = pos["symboltoken"],
                exchange         = pos.get("exchange", "NSE"),
                transaction_type = side,
                quantity         = abs(qty),
                order_type       = "MARKET",
                product_type     = "INTRADAY",
            )
            result = await self.place_order(req)
            if result.success:
                closed_count += 1
            else:
                logger.error(
                    f"Failed to close {pos['tradingsymbol']}: {result.message}"
                )

        logger.warning(f"Kill switch complete. Closed {closed_count} positions.")
        return {"success": True, "closed": closed_count, "is_paper": False}


# ── Singleton ─────────────────────────────────────────────────────────────────
angel_service = AngelOneService()