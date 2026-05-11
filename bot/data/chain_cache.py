"""Instrument and option-chain cache.

Responsibilities:
  - Periodically refresh the products list from /v2/products (every 5 min in prod).
  - Periodically refresh option Greeks + bid/ask from /v2/tickers.
  - Expose pickers used by strategies:
        get_atm_strike(symbol, expiry_bucket)
        get_strike_by_delta(symbol, expiry_bucket, target_delta, option_type)
        spread_pct(symbol)  # for the 8%-of-mid guardrail

Delta India option symbol convention (per docs):
    P-BTC-100000-130524  -> put,  BTC underlying, strike=100000, expiry=13-May-2024 (dd-mm-yy)
    C-ETH-3000-130524    -> call, ETH underlying, strike=3000,    expiry=13-May-2024
"""

from __future__ import annotations

import datetime as dt
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from loguru import logger

from bot.config.models import ExpiryBucket, Underlying
from bot.exchange.rest import DeltaRestClient

OptionType = Literal["call", "put"]

# Delta India product symbol format: C-BTC-100000-130524 / P-ETH-3000-130524
_SYMBOL_RE = re.compile(
    r"^(?P<opt>[CP])-(?P<underlying>BTC|ETH)-(?P<strike>\d+(?:\.\d+)?)-(?P<expiry>\d{6})$"
)


@dataclass(frozen=True)
class InstrumentRecord:
    """A single option product as known to the cache."""

    product_id: int
    symbol: str
    underlying: Underlying
    option_type: OptionType
    strike: float
    expiry: dt.datetime
    lot_size: float
    tick_size: float
    is_active: bool = True

    @property
    def days_to_expiry(self) -> float:
        return (self.expiry - dt.datetime.now(dt.UTC).replace(tzinfo=None)).total_seconds() / 86400.0


@dataclass
class QuoteSnapshot:
    """Most recent ticker data for an option symbol."""

    symbol: str
    bid: float | None = None
    ask: float | None = None
    mark_price: float | None = None
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None
    underlying_mark: float | None = None
    updated_at: float = field(default_factory=time.monotonic)

    @property
    def mid(self) -> float | None:
        if self.bid is not None and self.ask is not None and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.mark_price

    @property
    def spread_pct(self) -> float | None:
        mid = self.mid
        if mid is None or mid <= 0 or self.bid is None or self.ask is None:
            return None
        return (self.ask - self.bid) / mid


@dataclass(frozen=True)
class StrikeSelection:
    """Result of a chain query: an instrument + its current quote."""

    instrument: InstrumentRecord
    quote: QuoteSnapshot
    selection_reason: str  # e.g. "closest_delta_0.20", "atm", "otm_plus_one"


def parse_symbol(symbol: str) -> tuple[OptionType, Underlying, float, dt.datetime] | None:
    """Parse Delta India option symbol. Returns None if the format is unrecognised."""
    m = _SYMBOL_RE.match(symbol)
    if not m:
        return None
    opt_type: OptionType = "call" if m.group("opt") == "C" else "put"
    try:
        underlying = Underlying(m.group("underlying"))
    except ValueError:
        return None
    strike = float(m.group("strike"))
    expiry_raw = m.group("expiry")
    expiry = dt.datetime.strptime(expiry_raw, "%d%m%y").replace(hour=17, minute=30)
    return opt_type, underlying, strike, expiry


def bucket_for_expiry(now: dt.datetime, expiry: dt.datetime) -> ExpiryBucket | None:
    """Map an absolute expiry timestamp to one of D1/D2/W1/W2/W3 from `now`.

    Returns None for expiries that don't fit any bucket.
    """
    if expiry <= now:
        return None
    days = (expiry.date() - now.date()).days
    if days == 0:
        return ExpiryBucket.D1
    if days == 1:
        return ExpiryBucket.D2
    weekday = now.weekday()
    days_to_friday = (4 - weekday) % 7 or 7
    fridays = [now.date() + dt.timedelta(days=days_to_friday + 7 * i) for i in range(3)]
    if expiry.date() == fridays[0]:
        return ExpiryBucket.W1
    if expiry.date() == fridays[1]:
        return ExpiryBucket.W2
    if expiry.date() == fridays[2]:
        return ExpiryBucket.W3
    return None


class ChainCache:
    """In-memory option chain. Populate via `refresh_instruments` and `refresh_quotes`."""

    def __init__(self, rest: DeltaRestClient, *, products_ttl_seconds: float = 300.0) -> None:
        self._rest = rest
        self._products_ttl = products_ttl_seconds
        self._instruments_by_symbol: dict[str, InstrumentRecord] = {}
        self._quotes: dict[str, QuoteSnapshot] = {}
        self._products_last_refresh: float = 0.0

    @property
    def size(self) -> int:
        return len(self._instruments_by_symbol)

    def all_instruments(self) -> list[InstrumentRecord]:
        return list(self._instruments_by_symbol.values())

    def get_instrument(self, symbol: str) -> InstrumentRecord | None:
        return self._instruments_by_symbol.get(symbol)

    def get_quote(self, symbol: str) -> QuoteSnapshot | None:
        return self._quotes.get(symbol)

    def upsert_quote(self, snapshot: QuoteSnapshot) -> None:
        """Used by the WS client to push updates into the cache."""
        self._quotes[snapshot.symbol] = snapshot

    async def refresh_instruments(self, *, force: bool = False) -> int:
        now = time.monotonic()
        if not force and now - self._products_last_refresh < self._products_ttl:
            return self.size
        products = await self._rest.get_products(contract_types=["call_options", "put_options"])
        new: dict[str, InstrumentRecord] = {}
        for p in products:
            try:
                record = _product_to_record(p)
            except (KeyError, ValueError, TypeError) as exc:
                logger.debug("skip malformed product: {} ({})", p.get("symbol"), exc)
                continue
            if record is None:
                continue
            new[record.symbol] = record
        self._instruments_by_symbol = new
        self._products_last_refresh = now
        logger.info("chain cache: refreshed {} instruments", len(new))
        return len(new)

    async def refresh_quotes(self) -> int:
        tickers = await self._rest.get_tickers(contract_types=["call_options", "put_options"])
        updated = 0
        for t in tickers:
            snap = _ticker_to_snapshot(t)
            if snap is None:
                continue
            self._quotes[snap.symbol] = snap
            updated += 1
        logger.debug("chain cache: refreshed {} quotes", updated)
        return updated

    def instruments_for(
        self,
        underlying: Underlying,
        option_type: OptionType,
        bucket: ExpiryBucket,
        now: dt.datetime | None = None,
    ) -> list[InstrumentRecord]:
        now = now or dt.datetime.now(dt.UTC).replace(tzinfo=None)
        out: list[InstrumentRecord] = []
        for inst in self._instruments_by_symbol.values():
            if not inst.is_active or inst.underlying != underlying or inst.option_type != option_type:
                continue
            if bucket_for_expiry(now, inst.expiry) != bucket:
                continue
            out.append(inst)
        out.sort(key=lambda x: x.strike)
        return out

    def get_atm_strike(
        self,
        underlying: Underlying,
        option_type: OptionType,
        bucket: ExpiryBucket,
        spot_price: float,
        *,
        offset: int = 0,
        now: dt.datetime | None = None,
    ) -> StrikeSelection | None:
        """Return the ATM (offset=0) or ATM+offset strikes' StrikeSelection."""
        instruments = self.instruments_for(underlying, option_type, bucket, now)
        if not instruments:
            return None
        sorted_by_distance = sorted(instruments, key=lambda x: abs(x.strike - spot_price))
        atm = sorted_by_distance[0]
        if offset == 0:
            chosen = atm
            reason = "atm"
        else:
            idx_in_strike_order = next(
                i for i, inst in enumerate(instruments) if inst.symbol == atm.symbol
            )
            target_idx = idx_in_strike_order + offset
            if not 0 <= target_idx < len(instruments):
                return None
            chosen = instruments[target_idx]
            reason = f"atm{'+' if offset > 0 else ''}{offset}"
        quote = self._quotes.get(chosen.symbol)
        if quote is None:
            return None
        return StrikeSelection(instrument=chosen, quote=quote, selection_reason=reason)

    def get_strike_by_delta(
        self,
        underlying: Underlying,
        option_type: OptionType,
        bucket: ExpiryBucket,
        target_delta: float,
        *,
        delta_min: float | None = None,
        delta_max: float | None = None,
        now: dt.datetime | None = None,
    ) -> StrikeSelection | None:
        """Pick the listed strike whose live |delta| is closest to `target_delta`.

        `target_delta` is the *absolute* delta value (e.g. 0.20). For puts the sign convention
        is handled internally: we compare against |quote.delta|. If `delta_min`/`delta_max` are
        provided, the picked strike's |delta| must fall inside [delta_min, delta_max].
        """
        candidates: list[tuple[float, StrikeSelection]] = []
        for inst in self.instruments_for(underlying, option_type, bucket, now):
            quote = self._quotes.get(inst.symbol)
            if quote is None or quote.delta is None:
                continue
            abs_delta = abs(quote.delta)
            if delta_min is not None and abs_delta < delta_min:
                continue
            if delta_max is not None and abs_delta > delta_max:
                continue
            distance = abs(abs_delta - target_delta)
            selection = StrikeSelection(
                instrument=inst,
                quote=quote,
                selection_reason=f"closest_delta_{target_delta:.3f}",
            )
            candidates.append((distance, selection))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]

    def spread_pct(self, symbol: str) -> float | None:
        q = self._quotes.get(symbol)
        return q.spread_pct if q is not None else None


def _product_to_record(p: dict[str, Any]) -> InstrumentRecord | None:
    """Coerce a /v2/products dict into an InstrumentRecord, or None if it's not an option."""
    contract_type = p.get("contract_type")
    if contract_type not in {"call_options", "put_options"}:
        return None
    symbol = str(p["symbol"])
    parsed = parse_symbol(symbol)
    if parsed is None:
        return None
    opt_type, underlying, strike, expiry = parsed
    return InstrumentRecord(
        product_id=int(p["id"]),
        symbol=symbol,
        underlying=underlying,
        option_type=opt_type,
        strike=strike,
        expiry=expiry,
        lot_size=float(p.get("contract_value") or p.get("lot_size") or 1.0),
        tick_size=float(p.get("tick_size") or 0.5),
        is_active=p.get("state", "live") in {"live", "operational"},
    )


def _ticker_to_snapshot(t: dict[str, Any]) -> QuoteSnapshot | None:
    symbol = t.get("symbol")
    if not isinstance(symbol, str):
        return None
    greeks = t.get("greeks") or {}
    quotes = t.get("quotes") or {}
    return QuoteSnapshot(
        symbol=symbol,
        bid=_as_float(quotes.get("best_bid") or t.get("best_bid")),
        ask=_as_float(quotes.get("best_ask") or t.get("best_ask")),
        mark_price=_as_float(t.get("mark_price")),
        iv=_as_float(greeks.get("iv") or t.get("iv")),
        delta=_as_float(greeks.get("delta")),
        gamma=_as_float(greeks.get("gamma")),
        theta=_as_float(greeks.get("theta")),
        vega=_as_float(greeks.get("vega")),
        rho=_as_float(greeks.get("rho")),
        underlying_mark=_as_float(t.get("spot_price") or t.get("underlying_mark")),
    )


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def synthesise_quotes(items: Iterable[QuoteSnapshot]) -> dict[str, QuoteSnapshot]:
    """Helper used by tests + dry-run shim to seed a ChainCache without hitting the wire."""
    return {q.symbol: q for q in items}
