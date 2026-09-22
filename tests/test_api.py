import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from algotrader.api.app import create_app
from algotrader.engine import TradingEngine
from algotrader.events import EventBus
from algotrader.market_calendar import ManualClock, MarketCalendar
from algotrader.persistence import Repository
from algotrader.risk import RiskLimits, RiskManager
from algotrader.runtime import Runtime
from tests.conftest import INST, MONDAY_10AM, TOKEN, FakeBroker, FakeMarketData, ScriptedStrategy, make_settings


class FakeClient:
    is_connected = True

    async def login(self):
        pass

    async def logout(self):
        pass


class FakeFeed:
    connected = False

    def start(self, instruments):
        pass

    def stop(self):
        pass


@pytest.fixture
def client(tmp_path, calendar: MarketCalendar):
    settings = make_settings()

    def factory(s):
        clock = ManualClock(MONDAY_10AM)
        md = FakeMarketData()
        md.prices["RELIANCE"] = 1000.0
        bus = EventBus()
        repo = Repository(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}", "paper")
        engine = TradingEngine(s, ScriptedStrategy(), RiskManager(RiskLimits.from_settings(s), calendar),
                               FakeBroker(md.last_price), md, repo, bus, calendar, clock, [INST])
        return Runtime(s, engine, FakeClient(), FakeFeed(), repo, bus, calendar, clock, md)  # type: ignore[arg-type]

    with TestClient(create_app(settings, factory)) as c:
        yield c


AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_refuses_to_start_without_token():
    with pytest.raises(RuntimeError, match="API_AUTH_TOKEN"):
        create_app(make_settings(api_auth_token=""))


def test_health_is_public(client):
    assert client.get("/api/health").json()["status"] == "running"


@pytest.mark.parametrize("method,path", [
    ("get", "/api/v1/status"), ("get", "/api/v1/dashboard"), ("post", "/api/v1/bot/start"),
    ("post", "/api/v1/bot/kill-switch"), ("post", "/api/v1/watchlist"), ("post", "/api/v1/auth/login"),
])
def test_endpoints_require_token(client, method, path):
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_status_and_dashboard(client):
    s = client.get("/api/v1/status", headers=AUTH).json()
    assert s["paper_trading"] is True and s["running"] is False
    d = client.get("/api/v1/dashboard", headers=AUTH).json()
    assert d["watchlist"][0]["symbol"] == "RELIANCE"


def test_start_stop_resume_kill(client):
    assert client.post("/api/v1/bot/start", headers=AUTH).json()["success"]
    assert client.get("/api/v1/status", headers=AUTH).json()["running"]
    assert client.post("/api/v1/bot/stop", headers=AUTH).json()["success"]
    k = client.post("/api/v1/bot/kill-switch", headers=AUTH).json()
    assert k["success"] and k["positions_closed"] == 0
    assert client.post("/api/v1/bot/start", headers=AUTH).status_code == 409
    assert client.post("/api/v1/bot/resume", headers=AUTH).json()["success"]


def test_watchlist_validation(client):
    bad = client.post("/api/v1/watchlist", headers=AUTH, json=[{"symbol": "X", "token": "abc"}])
    assert bad.status_code == 422
    ok = client.post("/api/v1/watchlist", headers=AUTH, json=[{"symbol": "TCS", "token": "11536"}])
    assert ok.json()["symbols"] == ["TCS"]


def test_websocket_requires_token(client):
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/api/websocket?token=nope") as ws:
        ws.receive_text()
    with client.websocket_connect(f"/api/websocket?token={TOKEN}") as ws:
        assert ws.receive_json()["event"] == "connected"
        assert ws.receive_json()["event"] == "bot_status"
