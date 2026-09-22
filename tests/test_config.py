import json
from datetime import time

import pytest
from pydantic import ValidationError

from algotrader.config import Instrument, TradingMode, load_watchlist
from tests.conftest import TOKEN, make_settings

LIVE_CREDS = {
    "paper_trading": False,
    "angel_api_key": "key123",
    "angel_client_id": "C1",
    "angel_password": "pw12",
    "angel_totp_secret": "JBSWY3DPEHPK3PXP",
}


def test_defaults_are_paper_and_localhost():
    s = make_settings()
    assert s.mode is TradingMode.PAPER
    assert s.host == "127.0.0.1"


def test_live_requires_explicit_acknowledgement():
    with pytest.raises(ValidationError, match="LIVE_TRADING_ACKNOWLEDGED"):
        make_settings(**LIVE_CREDS)


def test_live_requires_long_api_token():
    with pytest.raises(ValidationError, match="API_AUTH_TOKEN"):
        make_settings(**LIVE_CREDS, live_trading_acknowledged=True, api_auth_token="short")


def test_live_requires_all_credentials():
    creds = dict(LIVE_CREDS, angel_totp_secret="")
    with pytest.raises(ValidationError, match="ANGEL_TOTP_SECRET"):
        make_settings(**creds, live_trading_acknowledged=True)


def test_live_ok_when_fully_configured():
    s = make_settings(**LIVE_CREDS, live_trading_acknowledged=True, api_auth_token=TOKEN)
    assert s.mode is TradingMode.LIVE


def test_session_times_must_be_ordered():
    with pytest.raises(ValidationError, match="session times"):
        make_settings(entry_cutoff=time(15, 20))


def test_risk_per_trade_cannot_exceed_capital():
    with pytest.raises(ValidationError, match="max_risk_per_trade"):
        make_settings(max_capital_per_trade=1000, max_risk_per_trade=2000)


def test_secrets_are_masked_in_repr():
    s = make_settings(gemini_api_key="super-secret-value")
    assert "super-secret-value" not in repr(s)
    assert "super-secret-value" in s.secret_values()


def test_cors_origins_parse_from_csv():
    s = make_settings(cors_origins="http://a.test, http://b.test")
    assert s.cors_origins == ["http://a.test", "http://b.test"]


def test_instrument_defaults_nse_trading_symbol():
    assert Instrument(symbol="reliance", token="2885").broker_symbol == "RELIANCE-EQ"
    assert Instrument(symbol="NIFTY", token="1", exchange="NFO").broker_symbol == "NIFTY"


def test_instrument_rejects_non_numeric_token():
    with pytest.raises(ValidationError):
        Instrument(symbol="X", token="abc")


def test_watchlist_rejects_duplicates(tmp_path):
    p = tmp_path / "w.json"
    p.write_text(json.dumps([{"symbol": "A", "token": "1"}, {"symbol": "A", "token": "2"}]))
    with pytest.raises(ValueError, match="duplicate"):
        load_watchlist(p)


def test_repo_watchlist_is_valid():
    items = load_watchlist(__import__("pathlib").Path("config/watchlist.json"))
    assert items and all(i.broker_symbol.endswith("-EQ") for i in items)
