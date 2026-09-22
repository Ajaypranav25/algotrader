"""
Persistence: audit trail of trades and signals, plus daily PnL.

The database is an audit log and a crash-recovery aid; the in-memory
RiskManager is the source of truth while the process runs.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from algotrader.domain import ExitReason, Position, Signal


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TradeRow(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(10), index=True)
    trading_day: Mapped[str] = mapped_column(String(10), index=True)
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    initial_stop: Mapped[float] = mapped_column(Float)
    target: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(12), index=True)  # OPEN / CLOSED / ORPHANED
    exit_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    entry_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exit_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    strategy: Mapped[str] = mapped_column(String(30))
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SignalRow(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    strategy: Mapped[str] = mapped_column(String(30))
    action: Mapped[str] = mapped_column(String(4))
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    target: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reference_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    decision: Mapped[str] = mapped_column(String(200), default="")
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    candles: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class DailyPnlRow(Base):
    __tablename__ = "daily_pnl"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(10))
    date: Mapped[str] = mapped_column(String(10), index=True)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    winning_trades: Mapped[int] = mapped_column(Integer, default=0)
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True)


def _ensure_sqlite_dir(url: str) -> None:
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database and parsed.database != ":memory:":
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)


class Repository:
    def __init__(self, database_url: str, mode: str) -> None:
        _ensure_sqlite_dir(database_url)
        self.engine: AsyncEngine = create_async_engine(database_url)
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.mode = mode

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    def session(self) -> AsyncSession:
        return self._sessions()

    # ── trades ───────────────────────────────────────────────────────────────
    async def record_entry(self, pos: Position, day: date, strategy: str, rationale: str) -> int:
        async with self._sessions() as s, s.begin():
            row = TradeRow(
                mode=self.mode, trading_day=day.isoformat(), symbol=pos.symbol, side=pos.side.value,
                quantity=pos.quantity, entry_price=pos.entry_price, stop_loss=pos.stop_loss,
                initial_stop=pos.initial_stop, target=pos.target, fees=pos.entry_fees, status="OPEN",
                entry_order_id=pos.entry_order_id, strategy=strategy, rationale=rationale,
            )
            s.add(row)
            await s.flush()
            return row.id

    async def record_exit(self, trade_id: int, exit_price: float, pnl: float, fees: float,
                          reason: ExitReason, order_id: str | None, final_stop: float) -> None:
        async with self._sessions() as s, s.begin():
            row = await s.get(TradeRow, trade_id)
            if row is None:
                return
            row.exit_price, row.pnl, row.status = exit_price, pnl, "CLOSED"
            row.fees = (row.fees or 0.0) + fees
            row.exit_reason, row.exit_order_id = reason.value, order_id
            row.stop_loss, row.closed_at = final_stop, _utcnow()

    async def open_trades(self) -> list[TradeRow]:
        async with self._sessions() as s:
            res = await s.execute(select(TradeRow).where(TradeRow.mode == self.mode, TradeRow.status == "OPEN"))
            return list(res.scalars())

    async def mark_orphaned(self, trade_ids: list[int]) -> None:
        if not trade_ids:
            return
        async with self._sessions() as s, s.begin():
            await s.execute(update(TradeRow).where(TradeRow.id.in_(trade_ids)).values(status="ORPHANED"))

    async def recent_trades(self, limit: int = 50, status: str | None = None) -> list[TradeRow]:
        async with self._sessions() as s:
            q = select(TradeRow).where(TradeRow.mode == self.mode).order_by(TradeRow.created_at.desc()).limit(limit)
            if status:
                q = q.where(TradeRow.status == status)
            return list((await s.execute(q)).scalars())

    # ── signals ──────────────────────────────────────────────────────────────
    async def record_signal(self, sig: Signal, decision: str, latency_ms: int | None, candles: int) -> None:
        async with self._sessions() as s, s.begin():
            s.add(SignalRow(
                symbol=sig.symbol, strategy=sig.strategy, action=sig.action.value, stop_loss=sig.stop_loss,
                target=sig.target, confidence=sig.confidence, reference_price=sig.reference_price,
                rationale=sig.rationale[:2000], decision=decision[:200], latency_ms=latency_ms, candles=candles,
            ))

    async def recent_signals(self, limit: int = 20) -> list[SignalRow]:
        async with self._sessions() as s:
            q = select(SignalRow).order_by(SignalRow.created_at.desc()).limit(limit)
            return list((await s.execute(q)).scalars())

    # ── daily pnl ────────────────────────────────────────────────────────────
    async def upsert_daily(self, day: date, realized: float, trades: int, wins: int) -> None:
        async with self._sessions() as s, s.begin():
            res = await s.execute(select(DailyPnlRow).where(
                DailyPnlRow.mode == self.mode, DailyPnlRow.date == day.isoformat()))
            row = res.scalar_one_or_none()
            if row is None:
                row = DailyPnlRow(mode=self.mode, date=day.isoformat(), is_paper=self.mode == "paper")
                s.add(row)
            row.realized_pnl, row.total_trades, row.winning_trades = realized, trades, wins

    async def daily_history(self, limit: int = 30) -> list[DailyPnlRow]:
        async with self._sessions() as s:
            q = (select(DailyPnlRow).where(DailyPnlRow.mode == self.mode)
                 .order_by(DailyPnlRow.date.desc()).limit(limit))
            return list((await s.execute(q)).scalars())
