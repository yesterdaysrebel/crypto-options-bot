"""ATM IV history and percentile ranks for desk / strategy filters."""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from typing import Literal

from loguru import logger
from sqlalchemy import select

from bot.config.models import ExpiryBucket, Underlying
from bot.data.candles import percentile_rank
from bot.data.chain_cache import ChainCache
from bot.storage.db import Database
from bot.storage.models import IvSnapshot

IvPercentileReason = Literal["ok", "iv_history_cold", "missing_current_iv"]

_DEFAULT_BUCKETS = (ExpiryBucket.D1, ExpiryBucket.D2, ExpiryBucket.W1)
_SAMPLE_INTERVAL_S = 300.0
_MIN_SAMPLES = 20
_LOOKBACK_DAYS = 90


@dataclass(frozen=True)
class IvPercentileResult:
    percentile: float | None
    reason: IvPercentileReason
    n_samples: int = 0


class IvHistoryStore:
    """Persist ATM IV samples and compute percentile ranks vs rolling history."""

    def __init__(
        self,
        db: Database,
        *,
        sample_interval_s: float = _SAMPLE_INTERVAL_S,
        min_samples: int = _MIN_SAMPLES,
        lookback_days: int = _LOOKBACK_DAYS,
    ) -> None:
        self._db = db
        self._sample_interval_s = sample_interval_s
        self._min_samples = min_samples
        self._lookback_days = lookback_days
        self._last_sample_mono: dict[tuple[str, str], float] = {}

    async def record_from_chain(
        self,
        chain: ChainCache,
        underlying_marks: dict[Underlying, float],
        now: dt.datetime,
    ) -> int:
        rows: list[IvSnapshot] = []
        for underlying in Underlying:
            spot = underlying_marks.get(underlying)
            if spot is None:
                continue
            for bucket in _DEFAULT_BUCKETS:
                key = (underlying.value, bucket.value)
                if not self._should_sample(key):
                    continue
                sampled = _atm_iv_sample(chain, underlying, bucket, spot, now)
                if sampled is None:
                    continue
                atm_iv, expiry_date = sampled
                rows.append(
                    IvSnapshot(
                        ts=now,
                        underlying=underlying.value,
                        expiry_bucket=bucket.value,
                        expiry_date=expiry_date,
                        atm_iv=atm_iv,
                    )
                )
        if not rows:
            return 0
        async with self._db.session() as session:
            session.add_all(rows)
        logger.debug("iv_history: recorded {} ATM IV samples", len(rows))
        return len(rows)

    async def iv_percentile(
        self,
        underlying: Underlying,
        bucket: ExpiryBucket,
        current_iv: float | None,
        *,
        now: dt.datetime | None = None,
    ) -> IvPercentileResult:
        if current_iv is None or not _finite(current_iv):
            return IvPercentileResult(None, "missing_current_iv", 0)
        now = now or dt.datetime.now(dt.UTC).replace(tzinfo=None)
        cutoff = now - dt.timedelta(days=self._lookback_days)
        async with self._db.session() as session:
            result = await session.execute(
                select(IvSnapshot.atm_iv).where(
                    IvSnapshot.underlying == underlying.value,
                    IvSnapshot.expiry_bucket == bucket.value,
                    IvSnapshot.ts >= cutoff,
                )
            )
            history = [float(v) for v in result.scalars().all() if _finite(v)]
        n = len(history)
        if n < self._min_samples:
            return IvPercentileResult(None, "iv_history_cold", n)
        pct = percentile_rank(float(current_iv), history)
        if not _finite(pct):
            return IvPercentileResult(None, "iv_history_cold", n)
        return IvPercentileResult(float(pct), "ok", n)

    def _should_sample(self, key: tuple[str, str]) -> bool:
        mono = time.monotonic()
        last = self._last_sample_mono.get(key, 0.0)
        if mono - last < self._sample_interval_s:
            return False
        self._last_sample_mono[key] = mono
        return True


def atm_iv_for_bucket(
    chain: ChainCache,
    underlying: Underlying,
    bucket: ExpiryBucket,
    spot: float,
    now: dt.datetime,
) -> float | None:
    """Current ATM implied vol for an underlying x expiry bucket (mean of call/put ATM IV)."""
    sampled = _atm_iv_sample(chain, underlying, bucket, spot, now)
    return sampled[0] if sampled is not None else None


def _atm_iv_sample(
    chain: ChainCache,
    underlying: Underlying,
    bucket: ExpiryBucket,
    spot: float,
    now: dt.datetime,
) -> tuple[float, dt.date] | None:
    ivs: list[float] = []
    expiry_date: dt.date | None = None
    for option_type in ("call", "put"):
        sel = chain.get_atm_strike(underlying, option_type, bucket, spot, now=now)
        if sel is None:
            continue
        expiry_date = sel.instrument.expiry.date()
        qiv = sel.quote.iv
        if qiv is not None and _finite(qiv):
            ivs.append(float(qiv))
    if not ivs or expiry_date is None:
        return None
    return sum(ivs) / len(ivs), expiry_date


def _finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))
