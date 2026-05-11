"""Tests for the boot-time order reconciler."""

from __future__ import annotations

import httpx
import pytest
from bot.config.settings import Settings
from bot.exchange.rest import DeltaRestClient
from bot.execution.reconcile import OrderReconciler
from bot.storage import (
    Database,
    Order,
    OrderState,
    Trade,
    TradeStatus,
    init_database,
)
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine


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


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        DELTA_API_KEY="test-key",
        DELTA_API_SECRET="test-secret",
        DELTA_REST_RPS=100,
        DELTA_ORDER_RPS=50,
    )


def _mock_rest(handler):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://api.india.delta.exchange", transport=transport)
    return DeltaRestClient(_settings(), client=client)


async def _seed_open_trade_with_open_order(db: Database, *, coid: str) -> tuple[int, int]:
    async with db.session() as session:
        trade = Trade(strategy_id="iron_condor", underlying="BTC", lots=1, status=TradeStatus.OPEN.value)
        session.add(trade)
        await session.flush()
        order = Order(
            strategy_id="iron_condor",
            trade_id=trade.id,
            leg_idx=0,
            client_order_id=coid,
            symbol="C-BTC-100000-150526",
            side="buy",
            order_type="limit",
            qty=1,
            state=OrderState.OPEN.value,
        )
        session.add(order)
        await session.flush()
        return trade.id, order.id


@pytest.mark.asyncio
async def test_reconcile_clean_when_db_and_exchange_agree(db: Database) -> None:
    await _seed_open_trade_with_open_order(db, coid="iron_condor-1-0-entry-abc")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v2/orders":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [
                        {
                            "id": 99999,
                            "client_order_id": "iron_condor-1-0-entry-abc",
                            "state": "open",
                            "symbol": "C-BTC-100000-150526",
                        }
                    ],
                },
            )
        return httpx.Response(404)

    rest = _mock_rest(handler)
    try:
        reconciler = OrderReconciler(db, rest)
        report = await reconciler.run()
    finally:
        await rest.aclose()
    assert not report.must_halt
    assert report.orders_checked == 1
    assert not report.mismatches


@pytest.mark.asyncio
async def test_reconcile_marks_disappeared_db_open_order_as_canceled(db: Database) -> None:
    _, order_id = await _seed_open_trade_with_open_order(db, coid="iron_condor-2-0-entry-xyz")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v2/orders" and req.method == "GET":
            params = dict(req.url.params)
            if "client_order_id" in params:
                # Singleton lookup: exchange does not know the order anymore.
                return httpx.Response(200, json={"success": True, "result": []})
            # The default "open orders" list.
            return httpx.Response(200, json={"success": True, "result": []})
        return httpx.Response(404)

    rest = _mock_rest(handler)
    try:
        reconciler = OrderReconciler(db, rest)
        report = await reconciler.run()
    finally:
        await rest.aclose()
    assert not report.must_halt
    assert report.orders_updated == 1
    async with db.session() as session:
        order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
    assert order.state == OrderState.CANCELED.value


@pytest.mark.asyncio
async def test_reconcile_promotes_filled_state_when_exchange_reports_filled(db: Database) -> None:
    _, order_id = await _seed_open_trade_with_open_order(db, coid="iron_condor-3-0-entry-fil")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v2/orders" and req.method == "GET":
            params = dict(req.url.params)
            if "client_order_id" in params:
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "result": [
                            {
                                "id": 7,
                                "client_order_id": params["client_order_id"],
                                "state": "filled",
                                "filled_size": 1,
                                "average_fill_price": 123.5,
                            }
                        ],
                    },
                )
            return httpx.Response(200, json={"success": True, "result": []})
        return httpx.Response(404)

    rest = _mock_rest(handler)
    try:
        report = await OrderReconciler(db, rest).run()
    finally:
        await rest.aclose()
    assert not report.must_halt
    async with db.session() as session:
        order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
    assert order.state == OrderState.FILLED.value
    assert order.filled_qty == 1
    assert order.filled_price == 123.5


@pytest.mark.asyncio
async def test_reconcile_refuses_to_start_on_foreign_open_order(db: Database) -> None:
    await _seed_open_trade_with_open_order(db, coid="iron_condor-4-0-entry-known")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v2/orders" and req.method == "GET":
            params = dict(req.url.params)
            if "client_order_id" in params:
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "result": [
                            {
                                "id": 8,
                                "client_order_id": params["client_order_id"],
                                "state": "open",
                            }
                        ],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [
                        {
                            "id": 1,
                            "client_order_id": "iron_condor-4-0-entry-known",
                            "state": "open",
                            "symbol": "C-BTC-100000-150526",
                        },
                        {
                            "id": 9999,
                            "client_order_id": "unknown-rogue-99",
                            "state": "open",
                            "symbol": "P-BTC-95000-150526",
                        },
                    ],
                },
            )
        return httpx.Response(404)

    rest = _mock_rest(handler)
    try:
        report = await OrderReconciler(db, rest).run()
    finally:
        await rest.aclose()
    assert report.must_halt
    assert any(m.kind == "foreign_open" for m in report.mismatches)
