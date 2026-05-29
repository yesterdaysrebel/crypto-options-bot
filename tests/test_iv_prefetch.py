"""IV percentile prefetch for the engine tick loop."""

from __future__ import annotations

import datetime as dt

import pytest
from bot.config.models import DirectionalConfig, ExpiryBucket, Underlying
from bot.desk.iv_history import IvHistoryStore
from bot.runtime.iv_prefetch import directional_needs_iv_prefetch, prefetch_directional_iv_percentiles
from bot.storage import init_database

from tests.strategy_fixtures import make_chain


@pytest.mark.asyncio
async def test_prefetch_returns_percentile_when_history_warm() -> None:
    db = await init_database(":memory:")
    try:
        await _run_prefetch_returns_percentile_when_history_warm(db)
    finally:
        await db.aclose()


async def _run_prefetch_returns_percentile_when_history_warm(db) -> None:
    iv_store = IvHistoryStore(db, min_samples=3)
    now = dt.datetime(2026, 5, 12, 4, 0, 0)
    spot = 100_000.0
    expiry = dt.datetime(2026, 5, 12, 12, 0, 0)
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=expiry,
        strikes=[99_500, 100_000, 100_500],
        spot=spot,
    )
    async with db.session() as session:
        from bot.storage.models import IvSnapshot

        for i, iv in enumerate([0.40, 0.45, 0.50, 0.55, 0.60]):
            session.add(
                IvSnapshot(
                    ts=now - dt.timedelta(hours=i),
                    underlying="BTC",
                    expiry_bucket="D1",
                    expiry_date=expiry.date(),
                    atm_iv=iv,
                )
            )

    dir_cfg = DirectionalConfig.model_validate(
        {
            "id": "directional",
            "enabled": True,
            "risk_weight": 0.6,
            "risk_per_trade_pct": 0.01,
            "max_lots_cap": 5,
            "desk": {"max_iv_percentile_long": 0.7},
        }
    )
    assert directional_needs_iv_prefetch(dir_cfg)
    result = await prefetch_directional_iv_percentiles(
        iv_store,
        chain,
        {Underlying.BTC: spot},
        dir_cfg,
        now=now,
    )
    d1 = result.get((Underlying.BTC, ExpiryBucket.D1))
    assert d1 is not None
    assert d1.reason == "ok"
    assert d1.percentile is not None


def test_directional_needs_iv_prefetch_false_when_unconfigured() -> None:
    cfg = DirectionalConfig.model_validate(
        {
            "id": "directional",
            "enabled": True,
            "risk_weight": 0.6,
            "risk_per_trade_pct": 0.01,
            "max_lots_cap": 5,
        }
    )
    assert not directional_needs_iv_prefetch(cfg)
