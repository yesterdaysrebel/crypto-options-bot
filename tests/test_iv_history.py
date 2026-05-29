"""IV history store: ATM sampling and percentile ranks."""

from __future__ import annotations

import datetime as dt

import pytest
from bot.config.models import ExpiryBucket, Underlying
from bot.data.chain_cache import ChainCache, QuoteSnapshot, synthesise_quotes
from bot.desk.iv_history import IvHistoryStore
from bot.storage.models import IvSnapshot
from sqlalchemy import select


def _seed_chain_with_iv(
    spot: float = 100_000.0,
    iv: float = 0.50,
    *,
    now: dt.datetime | None = None,
) -> ChainCache:
    from unittest.mock import AsyncMock

    from bot.data.chain_cache import _product_to_record

    now = now or dt.datetime(2026, 5, 18, 10, 0, 0)
    # Same-calendar-day expiry so bucket_for_expiry(now, expiry) == D1.
    expiry = dt.datetime(now.year, now.month, now.day, 12, 0, 0)
    if expiry <= now:
        expiry += dt.timedelta(hours=2)
    expiry_str = expiry.strftime("%d%m%y")

    cache = ChainCache(AsyncMock())
    cache._instruments_by_symbol = {}
    quotes: list[QuoteSnapshot] = []
    pid = 1
    for opt_letter, opt_type, delta in [("C", "call_options", 0.5), ("P", "put_options", -0.5)]:
        sym = f"{opt_letter}-BTC-{int(spot)}-{expiry_str}"
        record = _product_to_record(
            {
                "id": pid,
                "symbol": sym,
                "contract_type": opt_type,
                "contract_value": 0.001,
                "tick_size": 0.5,
                "state": "live",
            }
        )
        assert record is not None
        cache._instruments_by_symbol[sym] = record
        quotes.append(
            QuoteSnapshot(
                symbol=sym,
                bid=100.0,
                ask=102.0,
                mark_price=101.0,
                iv=iv,
                delta=delta,
            )
        )
        pid += 1
    cache._quotes = synthesise_quotes(quotes)
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


def test_should_sample_allows_first_sample_when_monotonic_is_low() -> None:
    """Regression: default last=0 made mono-last < interval on fresh CI runners."""
    from unittest.mock import AsyncMock

    store = IvHistoryStore(AsyncMock(), sample_interval_s=3600.0)
    key = (Underlying.BTC.value, ExpiryBucket.D1.value)
    assert store._should_sample(key) is True


@pytest.mark.asyncio
async def test_should_sample_respects_interval() -> None:
    from unittest.mock import AsyncMock

    store = IvHistoryStore(AsyncMock(), sample_interval_s=3600.0)
    key = (Underlying.BTC.value, ExpiryBucket.D1.value)
    assert store._should_sample(key) is True
    assert store._should_sample(key) is False


@pytest.mark.asyncio
async def test_record_from_chain_respects_sample_interval(db, monkeypatch) -> None:
    """Throttle is per (underlying, bucket); chain sampling is stubbed for determinism."""
    now = dt.datetime(2026, 5, 18, 10, 0, 0)
    store = IvHistoryStore(db, sample_interval_s=3600.0)
    chain = _seed_chain_with_iv(now=now)
    marks = {Underlying.BTC: 100_000.0}

    def _fake_atm_sample(
        _chain: ChainCache,
        _underlying: Underlying,
        _bucket: ExpiryBucket,
        _spot: float,
        _now: dt.datetime,
    ) -> tuple[float, dt.date] | None:
        return (0.50, _now.date())

    monkeypatch.setattr("bot.desk.iv_history._atm_iv_sample", _fake_atm_sample)

    n1 = await store.record_from_chain(chain, marks, now)
    n2 = await store.record_from_chain(chain, marks, now)
    assert n1 >= 1
    assert n2 == 0
    async with db.raw_session() as session:
        rows = (await session.execute(select(IvSnapshot))).scalars().all()
    assert len(rows) == n1
