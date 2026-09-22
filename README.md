# AlgoTrader

Intraday trading engine for NSE equities via Angel One SmartAPI, with pluggable
strategies (Google Gemini LLM or a deterministic EMA-crossover baseline), hard
risk limits, paper and live execution behind one interface, and a backtester
that reuses the exact same risk and execution code.

> **Paper trading is the default.** Live trading needs two explicit switches, a
> strong API token and full broker credentials — any missing piece fails at startup.

---

## Architecture

```
src/algotrader/
├── config.py            Settings (pydantic-settings, SecretStr), watchlist validation, live-mode gate
├── domain.py            Candle, Signal, OrderRequest/Result, Position (trailing-stop logic)
├── market_calendar.py   IST clock abstraction, session windows, NSE holidays
├── resilience.py        Jittered exponential backoff, async retry, rate limiters
├── observability.py     Console/JSON logging with secret redaction
├── data/
│   ├── feed.py          Angel tick stream: supervised reconnect, resubscribe, stale-feed watchdog
│   └── csv_source.py    CSV candles for backtests
├── strategies/          Strategy ABC · ema_crossover · gemini (schema-validated, timeout → HOLD)
├── risk.py              Pre-trade gate, position sizing, position book, daily loss halt
├── brokers/
│   ├── angel_client.py  Async SmartAPI wrapper (thread-safe, rate-limited, re-auth, no order retries)
│   ├── paper.py         Deterministic simulated fills (slippage + fees)
│   └── live.py          Live orders, confirmed by polling order status
├── engine.py            Signal loop + always-on protection loop, kill switch, restart reconciliation
├── persistence.py       SQLAlchemy async audit log: trades, signals, daily PnL
├── events.py            Per-subscriber pub/sub for the dashboard
├── runtime.py           Composition root (paper vs live wiring)
├── backtest.py          Bar-by-bar backtester
├── api/app.py           FastAPI REST + WebSocket, bearer-token auth
└── cli.py               serve · backtest · sample-data · verify-tokens
frontend/index.html      Dashboard (served at /)
config/                  watchlist.json, nse_holidays.json
tests/                   pytest suite (no network)
```

Data flows **market data → strategy → risk gate → broker → position book**. Strategies only
produce signals; they never size or place orders. The `RiskManager` is pure (no I/O), so the
live engine and the backtester apply identical rules.

### Two loops

| Loop | Interval | Runs when | Does |
|---|---|---|---|
| Signal | `GEMINI_ANALYSIS_INTERVAL` (300 s) | bot *started*, inside entry window, not halted | candles → strategy → risk gate → entry |
| Protection | `RISK_CHECK_INTERVAL_SECONDS` (1 s) | **always** while the process runs | trail stops, stop/target exits, daily-loss flatten, square-off at `SQUARE_OFF_TIME` |

`POST /bot/stop` stops new entries only. Open positions are *always* protected.

### Safety model

- **Positions come from confirmed fills.** Entries are recorded at the actual fill price and quantity. Live orders are polled until the broker reports `complete`.
- **Failed exits never drop a position.** The exit is retried with backoff, and after `MAX_CONSECUTIVE_EXIT_FAILURES` failures the engine halts and alerts.
- **Unknown order outcomes halt.** A transport error or fill-confirmation timeout halts new entries, because the order may have filled. Orders are never retried automatically, so a retry can't double-fill.
- **Daily loss limit counts realized and unrealized P&L.** When it's breached, the engine halts and flattens all positions. This halt can't be resumed the same day and clears automatically on the next trading day.
- **Position sizing** is `min(capital / price, risk_per_trade / |price − stop|)`. If that rounds to zero shares, the trade is rejected rather than forced to 1.
- **Signal validation.** A signal must have a stop on the correct side of price, meet the minimum reward:risk and confidence, and price must not have moved more than `MAX_ENTRY_DEVIATION_PCT` since the signal.
- **Stale data is never traded.** Ticks older than `STALE_PRICE_SECONDS` are ignored, with a throttled REST quote as the fallback. Strategies only ever see *completed* candles, never the still-forming bar.
- **Square-off at 15:10 IST** by default, before the broker's own auto square-off.
- **Restart recovery.** Today's open trades are restored from the DB, and in live mode cross-checked against broker positions (any mismatch halts). Open trades from earlier days are marked `ORPHANED`.
- **Kill switch** stops the bot, halts it, closes every tracked position and, in live mode, squares off any other *intraday* broker positions. Delivery holdings are never touched.
- **Security.** Every API and WebSocket endpoint needs `API_AUTH_TOKEN`, which is checked in constant time. The server binds `127.0.0.1` by default, and every configured secret is redacted from all logs.

---

## Quick start

Requires Python ≥ 3.11. [uv](https://docs.astral.sh/uv/) is recommended.

```bash
uv venv && uv pip install -e ".[dev]"
# or: python -m venv .venv && pip install -r requirements-dev.txt && pip install -e .
cp .env.example .env                                            # then edit .env
python -c "import secrets; print(secrets.token_urlsafe(48))"    # paste as API_AUTH_TOKEN
```

Run commands from the repository root: relative paths such as `config/` and `data/` resolve from there.

### 1. Backtest (offline, no credentials needed)

```bash
algotrader sample-data --out data/SAMPLE.csv --days 60          # synthetic random walk — smoke test only
algotrader backtest data/SAMPLE.csv --trades
algotrader backtest data/RELIANCE.csv data/INFY.csv --strategy ema_crossover
```

CSV columns: `timestamp,open,high,low,close,volume` (ISO timestamps; naive = IST). The file stem is the symbol.

The backtester's assumptions are deliberately conservative:
- A signal on bar *i*'s close fills at bar *i+1*'s **open**.
- When a bar touches both the stop and the target, the stop is assumed to hit first.
- A gap through the stop fills at the open.
- The trailing stop ratchets only after the bar's exit checks.
- Positions are squared off at the end of each day.
- Slippage (`SLIPPAGE_BPS`) and a flat fee (`FEE_PER_ORDER`) apply to every fill.

The output reports trades, win rate, profit factor, average trade and max drawdown.

Backtesting `--strategy gemini` works, but it makes one paid API call per bar, the results aren't reproducible, and the model may have seen the price history during training. Treat those results with suspicion.

### 2. Paper trading (live data, simulated fills)

```bash
# .env: PAPER_TRADING=true, Angel One credentials, API_AUTH_TOKEN, STRATEGY (+ GEMINI_API_KEY for gemini)
algotrader verify-tokens          # check watchlist tokens against Angel's scrip master
algotrader serve
```

Open <http://127.0.0.1:8000/>, paste your `API_AUTH_TOKEN` when prompted, then press **Start**.
Paper fills use the live price, adjusted for slippage and fees, and are recorded with `mode=paper`.

### 3. Going live — checklist

1. Run in paper mode for several weeks and review the `trades` and `signals` tables.
2. Set realistic limits: `MAX_CAPITAL_PER_TRADE`, `MAX_RISK_PER_TRADE` and `MAX_DAILY_LOSS`.
3. Fill in `config/nse_holidays.json` for the current year.
4. Enable live mode in `.env`:
   ```
   PAPER_TRADING=false
   LIVE_TRADING_ACKNOWLEDGED=true
   API_AUTH_TOKEN=<at least 32 random characters>
   ```
5. Start the server, confirm `"mode": "LIVE 🔴"` on `/api/v1/status`, and watch the first trades in the Angel One terminal.
6. Know where the kill switch is: `POST /api/v1/bot/kill-switch`, or the dashboard button.

### 4. Deployment

**Docker**

```bash
docker build -t algotrader .
docker run -d --name algotrader --restart unless-stopped --env-file .env \
  -v "$PWD/data:/app/data" -v "$PWD/logs:/app/logs" -p 127.0.0.1:8000:8000 algotrader
```

The image runs as a non-root user, takes secrets only from the environment, and logs JSON lines.

**systemd** (bare VM)

```ini
[Service]
WorkingDirectory=/opt/algotrader
EnvironmentFile=/opt/algotrader/.env
ExecStart=/opt/algotrader/.venv/bin/algotrader serve
Restart=always
RestartSec=5
```

Don't expose port 8000 to the internet directly. Put it behind a reverse proxy with TLS (Caddy or nginx) or an SSH tunnel. Keep the host clock NTP-synced, because session windows depend on it.

---

## API

Every `/api/v1/*` endpoint requires the header `Authorization: Bearer $API_AUTH_TOKEN`. The WebSocket takes the token as `?token=` instead.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Liveness check (public) |
| GET | `/api/v1/status` | Mode, halt state, P&L, connection status |
| GET | `/api/v1/dashboard` | Status + positions + recent trades + watchlist |
| GET | `/api/v1/positions` · `/trades` · `/signals` (alias `/gemini-logs`) · `/daily-pnl` | Read-only views |
| POST | `/api/v1/bot/start` · `/bot/stop` | Start or stop **new entries** |
| POST | `/api/v1/bot/kill-switch` | Emergency flatten + halt |
| POST | `/api/v1/bot/resume` | Clear a manual halt (not the daily-loss halt) |
| POST | `/api/v1/auth/login` | (Re)connect to the broker |
| GET/POST | `/api/v1/watchlist` | View or replace the watchlist (validated) |
| POST | `/api/v1/analyze/{symbol}` | Run the strategy once without trading |
| WS | `/api/websocket?token=…` | `bot_status`, `tick_update`, `gemini_signal`, `trade_opened`, `trade_closed`, `alert` |

Interactive docs are at `/docs`.

---

## Development

```bash
pytest                 # unit + integration tests, no network
ruff check src tests
mypy                   # strict mode
```

CI (`.github/workflows/ci.yml`) runs all three on Python 3.11 and 3.13.

Dependencies are declared in `pyproject.toml` and locked in `uv.lock`. `requirements.txt` and
`requirements-dev.txt` are exported from the lock. After changing dependencies:

```bash
uv lock
uv export --frozen --no-dev --no-emit-project --no-hashes -o requirements.txt
uv export --frozen --extra dev --no-emit-project --no-hashes -o requirements-dev.txt
```

### Adding a strategy

Subclass `algotrader.strategies.base.Strategy`, implement
`async generate(instrument, candles, ltp) -> Signal`, and register it in `strategies/__init__.py`.
`generate` must never raise; when data is bad or insufficient, return `self.hold(...)`.

---

## Known limitations

- Market orders only, MIS/INTRADAY product only, NSE/BSE cash equities.
- The ratcheted trailing stop is written to the DB only at exit. After a restart, the position resumes from its original stop.
- Live P&L uses the configured fee estimate; the broker's contract note is authoritative.
- The NSE holiday list must be maintained by hand.
- SQLite is fine for a single process. Use PostgreSQL (`postgresql+asyncpg://…`, `pip install asyncpg`) if you need concurrent readers.
- The v1 database schema is not migrated; the old DB is preserved as `data/legacy_v1.db`.

## Security note

Removing a committed `.env` from the working tree does **not** remove it from git history.
If credentials were ever committed, rotate them: the Angel One API key, password and TOTP secret, and the Gemini key.
