"""
Command line entry point.

    algotrader serve                         # API + engine (paper unless configured for live)
    algotrader backtest data/RELIANCE.csv    # offline backtest
    algotrader sample-data                   # write synthetic candles for a smoke-test backtest
    algotrader verify-tokens                 # check watchlist tokens against Angel's scrip master
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import random
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

from algotrader.config import Settings, StrategyName, get_settings, load_watchlist
from algotrader.observability import configure_logging

logger = logging.getLogger("algotrader.cli")


def _setup(settings: Settings, to_file: bool = True) -> None:
    configure_logging(settings.log_level, settings.log_format, settings.log_dir if to_file else None,
                      settings.secret_values())


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from algotrader.api.app import create_app

    settings = get_settings()
    _setup(settings)
    if settings.host not in {"127.0.0.1", "localhost", "::1"}:
        logger.warning("API is listening on %s — make sure it is behind TLS and a firewall", settings.host)
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None, access_log=False)
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from algotrader.backtest import Backtester
    from algotrader.data.csv_source import load_candles_csv
    from algotrader.runtime import build_calendar
    from algotrader.strategies import build_strategy

    settings = get_settings()
    _setup(settings, to_file=False)
    if not args.verbose:
        for name in ("algotrader.brokers", "algotrader.risk"):
            logging.getLogger(name).setLevel(logging.WARNING)
    strategy_name = StrategyName(args.strategy)
    if strategy_name is StrategyName.GEMINI:
        logger.warning("backtesting the Gemini strategy makes one paid API call per bar and is not "
                       "reproducible; the model may also have seen this price history in training")
    data = {Path(p).stem.upper(): load_candles_csv(Path(p)) for p in args.csv}
    bt = Backtester(settings, build_strategy(settings, strategy_name), build_calendar(settings))
    result = asyncio.run(bt.run(data))

    print(json.dumps(result.summary(), indent=2))
    if result.rejected_signals:
        print("rejected signals:", json.dumps(result.rejected_signals, indent=2))
    if args.trades:
        for t in result.trades:
            print(f"{t.entry_time:%Y-%m-%d %H:%M} {t.symbol:10} {t.side.value:4} {t.quantity:5} "
                  f"{t.entry_price:>10.2f} -> {t.exit_price:>10.2f}  {t.pnl:>+10.2f}  {t.exit_reason.value}")
    return 0


def cmd_sample_data(args: argparse.Namespace) -> int:
    """Geometric random walk on 15-minute NSE session bars — for smoke tests only, not research."""
    from algotrader.data.csv_source import write_candles_csv
    from algotrader.domain import Candle
    from algotrader.market_calendar import IST

    rng = random.Random(args.seed)  # noqa: S311
    price: float = args.start_price
    candles: list[Candle] = []
    day = datetime(2025, 1, 6, tzinfo=IST)
    while len({c.timestamp.date() for c in candles}) < args.days:
        if day.weekday() < 5:
            ts = datetime.combine(day.date(), time(9, 15), IST)
            while ts.time() < time(15, 30):
                o = price
                c = o * math.exp(rng.gauss(0.0, 0.004))
                h = max(o, c) * (1 + abs(rng.gauss(0, 0.0015)))
                low = min(o, c) * (1 - abs(rng.gauss(0, 0.0015)))
                candles.append(Candle(ts, round(o, 2), round(h, 2), round(low, 2), round(c, 2),
                                      rng.randint(10_000, 200_000)))
                price, ts = c, ts + timedelta(minutes=15)
        day += timedelta(days=1)
    out = Path(args.out)
    write_candles_csv(out, candles)
    print(f"wrote {len(candles)} candles to {out}")
    return 0


def cmd_verify_tokens(args: argparse.Namespace) -> int:
    import httpx

    settings = get_settings()
    _setup(settings, to_file=False)
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    master = httpx.get(url, timeout=60).json()
    by_token = {str(e["token"]): e for e in master}
    ok = True
    for inst in load_watchlist(settings.watchlist_file):
        rec = by_token.get(inst.token)
        if rec is None:
            print(f"MISSING   {inst.symbol:12} token={inst.token}")
            ok = False
            continue
        match = rec.get("symbol", "").upper() == inst.broker_symbol.upper() and \
            rec.get("exch_seg", "").upper() == inst.exchange.upper()
        ok &= match
        print(f"{'OK' if match else 'MISMATCH':9} {inst.symbol:12} token={inst.token:8} "
              f"master=({rec.get('symbol')}, {rec.get('exch_seg')})")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="algotrader")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the API and trading engine").set_defaults(fn=cmd_serve)

    bt = sub.add_parser("backtest", help="backtest a strategy on CSV candles")
    bt.add_argument("csv", nargs="+", help="CSV file(s); the file stem is used as the symbol")
    bt.add_argument("--strategy", default=StrategyName.EMA_CROSSOVER.value, choices=[s.value for s in StrategyName])
    bt.add_argument("--trades", action="store_true", help="print every trade")
    bt.add_argument("--verbose", action="store_true", help="log every simulated fill")
    bt.set_defaults(fn=cmd_backtest)

    sd = sub.add_parser("sample-data", help="generate synthetic candles")
    sd.add_argument("--out", default="data/SAMPLE.csv")
    sd.add_argument("--days", type=int, default=60)
    sd.add_argument("--start-price", type=float, default=1500.0)
    sd.add_argument("--seed", type=int, default=7)
    sd.set_defaults(fn=cmd_sample_data)

    sub.add_parser("verify-tokens", help="check watchlist tokens against the scrip master").set_defaults(
        fn=cmd_verify_tokens)

    args = parser.parse_args(argv)
    try:
        return int(args.fn(args))
    except Exception as exc:  # surface config errors cleanly
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
