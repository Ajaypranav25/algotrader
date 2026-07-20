# AlgoTrader — Gemini AI × Angel One SmartAPI

An automated intraday trading system for the Indian stock market powered by Google Gemini AI analysis and Angel One SmartAPI execution.

> ⚠️ **PAPER TRADING MODE IS ENABLED BY DEFAULT.** Set `PAPER_TRADING=false` in your `.env` only after thorough testing.

---

## Directory Structure

```
algotrader/
├── backend/
│   ├── main.py                    # FastAPI app entry point
│   ├── api/
│   │   ├── routes.py              # REST endpoints
│   │   └── websocket.py           # WebSocket push to frontend
│   ├── core/
│   │   ├── config.py              # Settings & env loading
│   │   ├── database.py            # SQLite/Postgres ORM
│   │   └── security.py            # Credential encryption
│   ├── services/
│   │   ├── angel_one.py           # SmartAPI auth + data + orders
│   │   ├── gemini_engine.py       # Gemini AI signal generation
│   │   ├── risk_manager.py        # Guardrails + position limits
│   │   ├── trading_loop.py        # Main orchestration loop
│   │   └── paper_trader.py        # Simulated execution
│   ├── models/
│   │   └── schemas.py             # Pydantic models
│   └── utils/
│       └── logger.py              # Structured logging
├── frontend/
│   ├── index.html
│   └── src/
│       ├── App.jsx
│       ├── components/
│       │   ├── StatusBar.jsx
│       │   ├── WatchlistPanel.jsx
│       │   ├── PositionsTable.jsx
│       │   ├── TradeLog.jsx
│       │   ├── GeminiAnalysis.jsx
│       │   ├── KillSwitch.jsx
│       │   └── PnLChart.jsx
│       └── hooks/
│           └── useWebSocket.js
├── config/
│   └── watchlist.json             # Stocks to monitor
├── .env.example
├── requirements.txt
└── README.md
```

## Quick Start

### 1. Install Python Dependencies
```bash
cd backend
pip install -r requirements.txt
```

### 2. Configure Environment
```bash
cp .env.example .env
# Edit .env with your real credentials
```

### 3. Set Up Watchlist
Edit `config/watchlist.json` with your preferred stocks.

### 4. Run Backend
```bash
cd backend
uvicorn main:app --reload --port 8000
```

### 5. Open Frontend
Open `frontend/index.html` in your browser (or use a local HTTP server).

---

## API Keys Required

| Service | Where to Get |
|---|---|
| Angel One API Key | [Angel One Developer Portal](https://smartapi.angelbroking.com/) |
| Angel One TOTP Secret | From Authenticator App setup in Angel One |
| Google Gemini API Key | [Google AI Studio](https://aistudio.google.com/) |

---

## Safety Features

- **Paper Trading Toggle** — All trades simulated by default
- **Max Capital Per Trade** — Hardcoded cap (default ₹10,000)
- **Daily Loss Limit** — Bot halts if daily loss exceeds threshold (default ₹5,000)
- **Trailing Stop-Loss** — Auto-managed for all open positions
- **Emergency Kill Switch** — One-click square-off of all positions
- **Market Hours Guard** — Bot only runs 9:15 AM – 3:15 PM IST

---


