"""Async token-bucket rate limiter.

Two buckets are constructed in `DeltaRestClient`: one for general REST traffic (100 req / 10s),
one for order-write traffic (50 req / 10s). WS messages are bounded separately in the WS client.
"""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """A monotonic-clock token bucket. Tokens refill continuously at `rate` per second.

    Usage:
        bucket = TokenBucket(rate=10.0, capacity=100)
        async with bucket:        # or await bucket.acquire()
            ...
    """

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        if rate <= 0:
            raise ValueError(f"rate must be > 0, got {rate}")
        self._rate = float(rate)
        self._capacity = float(capacity) if capacity is not None else float(max(1.0, rate))
        if self._capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {capacity}")
        self._tokens = self._capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def capacity(self) -> float:
        return self._capacity

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last_refill = now

    async def acquire(self, n: float = 1.0) -> None:
        if n <= 0:
            raise ValueError(f"n must be > 0, got {n}")
        if n > self._capacity:
            raise ValueError(f"n={n} exceeds bucket capacity={self._capacity}")
        async with self._lock:
            while True:
                self._refill_locked()
                if self._tokens >= n:
                    self._tokens -= n
                    return
                deficit = n - self._tokens
                wait_s = deficit / self._rate
                await asyncio.sleep(wait_s)

    async def __aenter__(self) -> TokenBucket:
        await self.acquire()
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def snapshot(self) -> dict[str, float]:
        """Diagnostic only. Not safely refilled under contention; for `make status`."""
        self._refill_locked()
        return {
            "rate": self._rate,
            "capacity": self._capacity,
            "tokens": self._tokens,
        }
