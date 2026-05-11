"""Tests for the storage layer.

AC for PR #5: `alembic upgrade head` creates all tables, downgrade is clean,
FK `legs.trade_id` enforced, and FK cascade-on-delete works.
"""

from __future__ import annotations

import datetime as dt

import pytest
from bot.storage import (
    Database,
    Decision,
    DecisionKind,
    DecisionReason,
    Instrument,
    Leg,
    LegStatus,
    NavHistory,
    Order,
    OrderSide,
    OrderState,
    OrderType,
    Trade,
    TradeStatus,
    init_database,
)
from sqlalchemy import event, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import selectinload


@pytest.fixture
async def db() -> Database:
    db_ = await init_database(":memory:")
    _enable_sqlite_fk(db_.engine)
    return db_


def _enable_sqlite_fk(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _enable(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


@pytest.mark.asyncio
async def test_all_expected_tables_created(db: Database) -> None:
    async with db.engine.connect() as conn:
        names = await conn.run_sync(lambda c: inspect(c).get_table_names())
    expected = {
        "instruments",
        "market_snapshots",
        "decisions",
        "signals",
        "orders",
        "legs",
        "trades",
        "daily_pnl",
        "nav_history",
    }
    assert expected.issubset(set(names)), f"missing tables: {expected - set(names)}"


@pytest.mark.asyncio
async def test_insert_decision_round_trip(db: Database) -> None:
    async with db.session() as session:
        session.add(
            Decision(
                strategy_id="directional",
                kind=DecisionKind.EVALUATE.value,
                symbol="C-BTC-100000-130524",
                underlying="BTC",
                passed=False,
                reason=DecisionReason.SPREAD_TOO_WIDE.value,
                feature_vector={"spread_pct": 0.09, "mid": 91.0},
            )
        )

    async with db.session() as session:
        rows = (await session.execute(select(Decision))).scalars().all()
    assert len(rows) == 1
    assert rows[0].reason == DecisionReason.SPREAD_TOO_WIDE.value
    assert rows[0].feature_vector is not None
    assert rows[0].feature_vector["spread_pct"] == 0.09


@pytest.mark.asyncio
async def test_trade_legs_cascade_delete(db: Database) -> None:
    async with db.session() as session:
        trade = Trade(strategy_id="iron_condor", underlying="BTC", lots=1)
        session.add(trade)
        await session.flush()
        for i in range(4):
            session.add(
                Leg(
                    trade_id=trade.id,
                    strategy_id="iron_condor",
                    leg_idx=i,
                    symbol=f"X-BTC-{i}",
                    side="buy" if i < 2 else "sell",
                    lots=1,
                )
            )

    async with db.session() as session:
        trades = (await session.execute(select(Trade).options(selectinload(Trade.legs)))).scalars().all()
        assert len(trades) == 1
        legs = (await session.execute(select(Leg))).scalars().all()
        assert len(legs) == 4
        await session.delete(trades[0])

    async with db.session() as session:
        legs = (await session.execute(select(Leg))).scalars().all()
    assert legs == []


@pytest.mark.asyncio
async def test_leg_with_invalid_trade_id_violates_fk(db: Database) -> None:
    async with db.raw_session() as session:
        await session.execute(text("PRAGMA foreign_keys=ON"))
        session.add(
            Leg(
                trade_id=9999,
                strategy_id="directional",
                leg_idx=0,
                symbol="C-BTC-100000-130524",
                side="buy",
                lots=1,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


@pytest.mark.asyncio
async def test_client_order_id_unique(db: Database) -> None:
    async with db.session() as session:
        session.add(
            Order(
                strategy_id="directional",
                client_order_id="abc123",
                symbol="C-BTC-100000-130524",
                side=OrderSide.BUY.value,
                order_type=OrderType.LIMIT_POST_ONLY.value,
                qty=1.0,
                state=OrderState.PENDING.value,
            )
        )

    async with db.raw_session() as session:
        session.add(
            Order(
                strategy_id="directional",
                client_order_id="abc123",
                symbol="C-BTC-100000-130524",
                side=OrderSide.SELL.value,
                order_type=OrderType.LIMIT_POST_ONLY.value,
                qty=1.0,
                state=OrderState.PENDING.value,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


@pytest.mark.asyncio
async def test_nav_history_unique_per_date(db: Database) -> None:
    today = dt.date.today()
    async with db.session() as session:
        session.add(NavHistory(trading_date=today, nav_inr=50000, peak_nav_inr=50000))

    async with db.raw_session() as session:
        session.add(NavHistory(trading_date=today, nav_inr=51000, peak_nav_inr=51000))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


@pytest.mark.asyncio
async def test_instrument_symbol_unique(db: Database) -> None:
    async with db.session() as session:
        session.add(
            Instrument(
                product_id=1,
                symbol="C-BTC-100000-130524",
                underlying="BTC",
                contract_type="call_options",
            )
        )

    async with db.raw_session() as session:
        session.add(
            Instrument(
                product_id=2,
                symbol="C-BTC-100000-130524",
                underlying="BTC",
                contract_type="call_options",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


@pytest.mark.asyncio
async def test_close_trade_with_legs_round_trip(db: Database) -> None:
    async with db.session() as session:
        trade = Trade(
            strategy_id="directional",
            underlying="BTC",
            entry_ts=dt.datetime(2026, 5, 12, 6, 0, 0),
            status=TradeStatus.OPEN.value,
            lots=5,
            premium_paid_inr=455.0,
            entry_iv=0.562,
        )
        session.add(trade)
        await session.flush()
        session.add(
            Leg(
                trade_id=trade.id,
                strategy_id="directional",
                leg_idx=0,
                symbol="C-BTC-100000-130524",
                option_type="call",
                strike=100000.0,
                side="buy",
                lots=5,
                entry_price=91.0,
                status=LegStatus.OPEN.value,
            )
        )

    async with db.session() as session:
        loaded = (await session.execute(select(Trade).options(selectinload(Trade.legs)))).scalar_one()
        loaded.exit_ts = dt.datetime(2026, 5, 12, 10, 0, 0)
        loaded.status = TradeStatus.CLOSED.value
        loaded.realised_pnl_inr = 670.0
        loaded.r_multiple = 1.47
        loaded.exit_reason = "trail_chandelier"
        leg = loaded.legs[0]
        leg.exit_price = 225.0
        leg.pnl_inr = 670.0
        leg.status = LegStatus.CLOSED.value

    async with db.session() as session:
        loaded = (await session.execute(select(Trade).options(selectinload(Trade.legs)))).scalar_one()
        assert loaded.status == TradeStatus.CLOSED.value
        assert loaded.realised_pnl_inr == 670.0
        assert len(loaded.legs) == 1
        assert loaded.legs[0].pnl_inr == 670.0
