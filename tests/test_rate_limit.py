"""Tests for the async token-bucket rate limiter."""

from __future__ import annotations

import asyncio
import time

import pytest
from bot.exchange.rate_limit import TokenBucket


@pytest.mark.asyncio
async def test_acquire_within_capacity_is_instant() -> None:
    bucket = TokenBucket(rate=100.0, capacity=10)
    start = time.monotonic()
    for _ in range(10):
        await bucket.acquire()
    elapsed = time.monotonic() - start
    assert elapsed < 0.05, f"10 acquires within capacity should be instant, took {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_acquire_beyond_capacity_blocks_for_refill() -> None:
    bucket = TokenBucket(rate=20.0, capacity=2)
    await bucket.acquire()
    await bucket.acquire()
    start = time.monotonic()
    await bucket.acquire()
    elapsed = time.monotonic() - start
    assert 0.03 <= elapsed <= 0.20, f"3rd acquire should wait ~50ms, got {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_acquire_n_greater_than_capacity_raises() -> None:
    bucket = TokenBucket(rate=10.0, capacity=5)
    with pytest.raises(ValueError, match="exceeds bucket capacity"):
        await bucket.acquire(10)


@pytest.mark.asyncio
async def test_concurrent_acquires_serialize_under_rate_limit() -> None:
    bucket = TokenBucket(rate=10.0, capacity=2)
    start = time.monotonic()
    await asyncio.gather(*(bucket.acquire() for _ in range(6)))
    elapsed = time.monotonic() - start
    assert 0.30 <= elapsed <= 0.80, (
        f"6 acquires at rate=10/s with capacity=2 should take ~0.4s, got {elapsed:.3f}s"
    )


def test_invalid_rate_raises() -> None:
    with pytest.raises(ValueError):
        TokenBucket(rate=0)
    with pytest.raises(ValueError):
        TokenBucket(rate=-1)


def test_snapshot_reports_state() -> None:
    bucket = TokenBucket(rate=5.0, capacity=10)
    snap = bucket.snapshot()
    assert snap["rate"] == 5.0
    assert snap["capacity"] == 10.0
    assert snap["tokens"] <= 10.0
