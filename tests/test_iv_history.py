"""IV history store: ATM sampling and percentile ranks."""

from __future__ import annotations

import datetime as dt

import pytest
from bot.config.models import ExpiryBucket, Underlying
from bot.data.chain_cache import ChainCache, QuoteSnapshot, synthesise_quotes
from bot.desk.iv_history import IvHistoryStore
from bot.storage.db import init_database
from bot.storage.models import IvSnapshot
from sqlalchemy import select


@pytest.fixture
async def db():
    database = await init_database(":memory:")
    yield database
    await database.aclose()


def _seed_chain_with_iv(spot: float = 100_000.0, iv: float = 0.50) -> ChainCache:
    from unittest.mock import AsyncMock

    cache = ChainCache(AsyncMock())
    today = dt.date.today()
    expiry_str = today.strftime("%d%m%y")
    sym = f"C-BTC-{int(spot)}-{expiry_str}"
    cache._instruments_by_symbol = {}
    from bot.data.chain_cache import _product_to_record

    product = {
        "id": 1,
        "symbol": sym,
        "contract_type": "call_options",
        "contract_value": 0.001,
        "tick_size": 0.5,
        "state": "live",
    }
    record = _product_to_record(product)
    assert record is not None
    cache._instruments_by_symbol[sym] = record
    cache._quotes = synthesise_quotes(
        [
            QuoteSnapshot(
                symbol=sym,
                bid=100.0,
                ask=102.0,
                mark_price=101.0,
                iv=iv,
                delta=0.5,
            )
        ]
    )
    return cache


@pytest.mark.asyncio
async def test_iv_percentile_cold_when_insufficient_history(db) -> None:
    store = IvHistoryStore(db, min_samples=20)
    result = await store.iv_percentile(Underlying.BTC, ExpiryBucket.D1, 0.55)
    assert result.percentile is None
    assert result.reason == "iv_history_cold"
    assert result.n_samples == 0


@pytest.mark.asyncio
async def test_iv_percentile_returns_rank_with_enough_samples(db) -> None:
    store = IvHistoryStore(db, min_samples=5, sample_interval_s=0.0)
    now = dt.datetime(2026, 5, 18, 10, 0, 0)
    async with db.session() as session:
        for i, iv in enumerate([0.40, 0.45, 0.50, 0.55, 0.60]):
            session.add(
                IvSnapshot(
                    ts=now - dt.timedelta(hours=i),
                    underlying=Underlying.BTC.value,
                    expiry_bucket=ExpiryBucket.D1.value,
                    expiry_date=now.date(),
                    atm_iv=iv,
                )
            )
    result = await store.iv_percentile(Underlying.BTC, ExpiryBucket.D1, 0.55, now=now)
    assert result.reason == "ok"
    assert result.percentile is not None
    assert 0.0 <= result.percentile <= 1.0
    assert result.n_samples == 5


@pytest.mark.asyncio
async def test_record_from_chain_respects_sample_interval(db) -> None:
    store = IvHistoryStore(db, sample_interval_s=3600.0)
    chain = _seed_chain_with_iv()
    marks = {Underlying.BTC: 100_000.0}
    now = dt.datetime(2026, 5, 18, 10, 0, 0)
    n1 = await store.record_from_chain(chain, marks, now)
    n2 = await store.record_from_chain(chain, marks, now)
    assert n1 >= 1
    assert n2 == 0
    async with db.raw_session() as session:
        rows = (await session.execute(select(IvSnapshot))).scalars().all()
    assert len(rows) == n1
