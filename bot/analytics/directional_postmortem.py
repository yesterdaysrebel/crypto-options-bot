"""Post-mortem: directional live attempts vs underlying price action (Delta candles).

Correlates SQLite trades/signals with historical 15m OHLC from Delta to estimate whether
spot moved for or against the intended long-premium direction (call = bullish, put = bearish).

Usage (on VPS):
  python -m bot.cli analyze-directional --since 2026-05-27 --dedupe
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bot.config.settings import Settings
from bot.exchange.rest import DeltaRestClient, DeltaRestError

UNDERLYING_CANDLE_SYMBOL = {
    "BTC": "BTCUSD",
    "ETH": "ETHUSD",
}

# Bars of 15m after entry to measure follow-through (15m .. 2h).
FORWARD_BARS = (1, 2, 4, 8)


@dataclass
class TradeAttempt:
    trade_id: int
    entry_ts: dt.datetime
    underlying: str
    status: str
    mode: str
    error: str | None
    intended_symbol: str | None
    intended_strike: float | None
    intended_premium_inr: float | None
    option_type: str | None  # call | put
    spot_at_signal: float | None
    ema_sep: float | None
    atr: float | None
    feature_vector: dict[str, Any] = field(default_factory=dict)


@dataclass
class MovementRow:
    attempt: TradeAttempt
    entry_spot: float
    spot_at_bar: dict[int, float]
    ret_pct_at_bar: dict[int, float]
    mfe_pct: float  # max favorable excursion (spot path)
    mae_pct: float  # max adverse excursion
    verdict_60m: str  # favorable | adverse | flat


def _parse_ts(raw: str) -> dt.datetime:
    # SQLite stores naive UTC in this deployment.
    return dt.datetime.fromisoformat(raw.replace("Z", ""))


def _option_type_from_symbol(symbol: str | None) -> str | None:
    if not symbol:
        return None
    if symbol.startswith("C-"):
        return "call"
    if symbol.startswith("P-"):
        return "put"
    return None


def load_attempts(
    db_path: Path,
    *,
    since: dt.datetime | None,
    until: dt.datetime | None,
    mode: str | None,
    status: str | None,
) -> list[TradeAttempt]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    clauses = ["t.strategy_id = 'directional'"]
    params: list[Any] = []
    if since is not None:
        clauses.append("t.entry_ts >= ?")
        params.append(since.isoformat(sep=" "))
    if until is not None:
        clauses.append("t.entry_ts < ?")
        params.append(until.isoformat(sep=" "))
    if mode:
        clauses.append("t.mode = ?")
        params.append(mode)
    if status:
        clauses.append("t.status = ?")
        params.append(status)
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"""
        SELECT t.id, t.entry_ts, t.underlying, t.status, t.mode, t.notes,
               s.intended_symbol, s.intended_strike, s.intended_premium_inr, s.feature_vector
        FROM trades t
        LEFT JOIN signals s ON s.id = t.signal_id
        WHERE {where}
        ORDER BY t.entry_ts ASC
        """,
        params,
    ).fetchall()
    conn.close()
    out: list[TradeAttempt] = []
    for r in rows:
        notes_raw = r["notes"]
        notes: dict[str, Any] = {}
        if notes_raw:
            try:
                notes = json.loads(notes_raw) if isinstance(notes_raw, str) else dict(notes_raw)
            except (TypeError, json.JSONDecodeError):
                notes = {}
        fv_raw = r["feature_vector"]
        fv: dict[str, Any] = {}
        if fv_raw:
            try:
                fv = json.loads(fv_raw) if isinstance(fv_raw, str) else dict(fv_raw)
            except (TypeError, json.JSONDecodeError):
                fv = {}
        sym = r["intended_symbol"]
        out.append(
            TradeAttempt(
                trade_id=int(r["id"]),
                entry_ts=_parse_ts(str(r["entry_ts"])),
                underlying=str(r["underlying"]),
                status=str(r["status"]),
                mode=str(r["mode"]),
                error=notes.get("error") if isinstance(notes.get("error"), str) else None,
                intended_symbol=sym,
                intended_strike=float(r["intended_strike"]) if r["intended_strike"] is not None else None,
                intended_premium_inr=float(r["intended_premium_inr"])
                if r["intended_premium_inr"] is not None
                else None,
                option_type=_option_type_from_symbol(sym),
                spot_at_signal=_as_float(fv.get("spot")),
                ema_sep=_as_float(fv.get("ema_sep")),
                atr=_as_float(fv.get("atr")),
                feature_vector=fv,
            )
        )
    return out


def dedupe_attempts(attempts: list[TradeAttempt], *, bucket_minutes: int = 15) -> list[TradeAttempt]:
    """One row per underlying + intended symbol + time bucket (drops retry storm duplicates)."""
    seen: dict[tuple[str, str, int], TradeAttempt] = {}
    bucket_sec = bucket_minutes * 60
    for a in attempts:
        sym = a.intended_symbol or ""
        bucket = int(a.entry_ts.timestamp()) // bucket_sec
        key = (a.underlying, sym, bucket)
        if key not in seen or a.trade_id < seen[key].trade_id:
            seen[key] = a
    return sorted(seen.values(), key=lambda x: x.entry_ts)


def _as_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _candle_time(c: dict[str, Any]) -> int:
    t = c.get("time")
    if isinstance(t, (int, float)):
        return int(t)
    return 0


def _spot_favorable(option_type: str | None, ret_pct: float, *, flat_bps: float = 5.0) -> str:
    if option_type not in ("call", "put"):
        return "unknown"
    if abs(ret_pct) * 100.0 < flat_bps:
        return "flat"
    if option_type == "call":
        return "favorable" if ret_pct > 0 else "adverse"
    return "favorable" if ret_pct < 0 else "adverse"


async def _fetch_candles(
    rest: DeltaRestClient,
    symbol: str,
    start: dt.datetime,
    end: dt.datetime,
) -> list[dict[str, Any]]:
    candles = await rest.get_candles(
        symbol,
        "15m",
        int(start.timestamp()),
        int(end.timestamp()),
    )
    return sorted(candles, key=_candle_time)


def _nearest_candle_index(candles: list[dict[str, Any]], entry_ts: dt.datetime) -> int | None:
    if not candles:
        return None
    target = int(entry_ts.timestamp())
    best_i = 0
    best_d = abs(_candle_time(candles[0]) - target)
    for i, c in enumerate(candles):
        d = abs(_candle_time(c) - target)
        if d < best_d:
            best_d = d
            best_i = i
    return best_i


def analyze_movement(
    attempt: TradeAttempt,
    candles: list[dict[str, Any]],
) -> MovementRow | None:
    entry_spot = attempt.spot_at_signal
    if entry_spot is None or entry_spot <= 0:
        idx = _nearest_candle_index(candles, attempt.entry_ts)
        if idx is None:
            return None
        entry_spot = float(candles[idx].get("close") or 0)
    if entry_spot <= 0:
        return None

    idx = _nearest_candle_index(candles, attempt.entry_ts)
    if idx is None:
        return None

    spot_at_bar: dict[int, float] = {}
    ret_at_bar: dict[int, float] = {}
    mfe = 0.0
    mae = 0.0
    end_i = min(len(candles) - 1, idx + max(FORWARD_BARS))

    for j in range(idx + 1, end_i + 1):
        close = float(candles[j].get("close") or entry_spot)
        high = float(candles[j].get("high") or close)
        low = float(candles[j].get("low") or close)
        bars_fwd = j - idx
        if bars_fwd in FORWARD_BARS:
            spot_at_bar[bars_fwd] = close
            ret_at_bar[bars_fwd] = (close - entry_spot) / entry_spot
        ret_high = (high - entry_spot) / entry_spot
        ret_low = (low - entry_spot) / entry_spot
        if attempt.option_type == "call":
            mfe = max(mfe, ret_high)
            mae = min(mae, ret_low)
        elif attempt.option_type == "put":
            mfe = max(mfe, -ret_low)
            mae = min(mae, -ret_high)
        else:
            mfe = max(mfe, abs(ret_high), abs(ret_low))
            mae = mae

    ret_60 = ret_at_bar.get(4)
    verdict = _spot_favorable(attempt.option_type, ret_60 if ret_60 is not None else 0.0)
    return MovementRow(
        attempt=attempt,
        entry_spot=entry_spot,
        spot_at_bar=spot_at_bar,
        ret_pct_at_bar=ret_at_bar,
        mfe_pct=mfe * 100.0,
        mae_pct=mae * 100.0,
        verdict_60m=verdict,
    )


async def fetch_delta_orders_for_trade(
    rest: DeltaRestClient,
    trade_id: int,
) -> list[dict[str, Any]]:
    """Best-effort: list recent orders and filter by compact trade id suffix."""
    found: list[dict[str, Any]] = []
    suffix = f"-{trade_id}-"
    for state in ("open", "closed", None):
        params: dict[str, str] = {}
        if state:
            params["state"] = state
        try:
            env = await rest._request("GET", "/v2/orders", params=params, signed=True)
        except DeltaRestError:
            continue
        rows = env.get("result") or []
        if isinstance(rows, dict):
            rows = [rows]
        for o in rows:
            if not isinstance(o, dict):
                continue
            coid = str(o.get("client_order_id") or "")
            if coid.startswith("dr-") and suffix in coid:
                found.append(o)
    return found


@dataclass
class PostmortemReport:
    attempts_loaded: int
    attempts_analyzed: int
    movement_rows: list[MovementRow]
    exchange_orders: dict[int, list[dict[str, Any]]]
    positions: list[dict[str, Any]]
    errors: list[str]


async def build_report(
    db_path: Path,
    *,
    since: dt.datetime | None,
    until: dt.datetime | None,
    mode: str = "live",
    status: str | None = None,
    dedupe: bool = True,
    max_samples: int = 40,
    fetch_exchange: bool = True,
) -> PostmortemReport:
    attempts = load_attempts(db_path, since=since, until=until, mode=mode, status=status)
    if dedupe:
        attempts = dedupe_attempts(attempts)
    if max_samples > 0 and len(attempts) > max_samples:
        # Keep most recent samples
        attempts = attempts[-max_samples:]

    settings = Settings()
    movement_rows: list[MovementRow] = []
    exchange_orders: dict[int, list[dict[str, Any]]] = {}
    positions: list[dict[str, Any]] = []
    errors: list[str] = []

    candle_cache: dict[str, list[dict[str, Any]]] = {}

    async with DeltaRestClient(settings) as rest:
        if fetch_exchange:
            try:
                for p in await rest.get_positions():
                    sz = float(p.get("size") or 0)
                    if sz != 0:
                        positions.append(p)
            except DeltaRestError as exc:
                errors.append(f"get_positions: {exc}")

        for attempt in attempts:
            sym = UNDERLYING_CANDLE_SYMBOL.get(attempt.underlying)
            if not sym:
                errors.append(f"trade {attempt.trade_id}: unknown underlying {attempt.underlying}")
                continue

            window_start = attempt.entry_ts - dt.timedelta(hours=2)
            window_end = attempt.entry_ts + dt.timedelta(hours=3)
            cache_key = f"{sym}:{window_start.date()}:{window_end.date()}"
            if cache_key not in candle_cache:
                try:
                    candle_cache[cache_key] = await _fetch_candles(rest, sym, window_start, window_end)
                except DeltaRestError as exc:
                    errors.append(f"candles {sym}: {exc}")
                    candle_cache[cache_key] = []

            row = analyze_movement(attempt, candle_cache[cache_key])
            if row is not None:
                movement_rows.append(row)

            if fetch_exchange and attempt.trade_id >= 1000:
                try:
                    orders = await fetch_delta_orders_for_trade(rest, attempt.trade_id)
                    if orders:
                        exchange_orders[attempt.trade_id] = orders
                except DeltaRestError as exc:
                    errors.append(f"orders trade {attempt.trade_id}: {exc}")

    return PostmortemReport(
        attempts_loaded=len(attempts),
        attempts_analyzed=len(movement_rows),
        movement_rows=movement_rows,
        exchange_orders=exchange_orders,
        positions=positions,
        errors=errors,
    )


def _pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def format_report(report: PostmortemReport) -> str:
    lines: list[str] = [
        "# Directional post-mortem (DB + Delta candles)",
        "",
        f"- Samples analyzed: **{report.attempts_analyzed}** (from {report.attempts_loaded} deduped/load)",
        "",
    ]

    if report.positions:
        lines.append("## Open positions on Delta now")
        for p in report.positions:
            lines.append(
                f"- {p.get('product_symbol')}: size={p.get('size')} entry={p.get('entry_price')} "
                f"uPnL={p.get('unrealized_pnl')}"
            )
        lines.append("")
    else:
        lines.append("## Open positions on Delta now\n\nNone (size ≠ 0).\n")

    if report.movement_rows:
        by_verdict: dict[str, int] = defaultdict(int)
        by_und: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for row in report.movement_rows:
            by_verdict[row.verdict_60m] += 1
            by_und[row.attempt.underlying][row.verdict_60m] += 1

        lines.append("## Spot follow-through (~60m / 4x15m bars)")
        lines.append("")
        lines.append("For long premium: **call** wants spot up; **put** wants spot down.")
        lines.append("")
        total = len(report.movement_rows)
        for v in ("favorable", "adverse", "flat", "unknown"):
            n = by_verdict.get(v, 0)
            if n:
                lines.append(f"- {v}: {n} ({100.0 * n / total:.0f}%)")
        lines.append("")
        for und, counts in sorted(by_und.items()):
            lines.append(f"### {und}")
            for v, n in sorted(counts.items()):
                lines.append(f"- {v}: {n}")
            lines.append("")

        lines.append("## Per-attempt detail (most recent last)")
        lines.append("")
        lines.append(
            "| trade | entry (UTC) | und | opt | strike | prem INR | status | error | "
            "spot | 15m | 30m | 60m | 120m | MFE% | MAE% | 60m verdict |"
        )
        lines.append(
            "|------:|---------------|-----|-----|-------:|---------:|--------|-------|"
            "-----:|----:|----:|----:|-----:|-----:|-----:|------------|"
        )
        for row in report.movement_rows:
            a = row.attempt
            r1 = row.ret_pct_at_bar.get(1)
            r2 = row.ret_pct_at_bar.get(2)
            r4 = row.ret_pct_at_bar.get(4)
            r8 = row.ret_pct_at_bar.get(8)
            lines.append(
                f"| {a.trade_id} | {a.entry_ts.isoformat(sep=' ', timespec='seconds')} | {a.underlying} | "
                f"{a.option_type or '?'} | {a.intended_strike or ''} | {a.intended_premium_inr or ''} | "
                f"{a.status} | {(a.error or '')[:24]} | {row.entry_spot:.2f} | "
                f"{_pct(r1) if r1 is not None else '—'} | "
                f"{_pct(r2) if r2 is not None else '—'} | "
                f"{_pct(r4) if r4 is not None else '—'} | "
                f"{_pct(r8) if r8 is not None else '—'} | "
                f"{row.mfe_pct:+.2f} | {row.mae_pct:+.2f} | {row.verdict_60m} |"
            )
        lines.append("")

    if report.exchange_orders:
        lines.append("## Delta orders matched by `dr-*-{trade_id}-*`")
        lines.append("")
        for tid, orders in sorted(report.exchange_orders.items()):
            lines.append(f"### trade {tid}")
            for o in orders:
                lines.append(
                    f"- {o.get('client_order_id')}: {o.get('product_symbol')} {o.get('side')} "
                    f"state={o.get('state')} filled={o.get('filled_size')} avg={o.get('average_fill_price')}"
                )
            lines.append("")

    if report.errors:
        lines.append("## API / data warnings")
        for e in report.errors[:20]:
            lines.append(f"- {e}")
        if len(report.errors) > 20:
            lines.append(f"- ... and {len(report.errors) - 20} more")

    lines.append("")
    lines.append(
        "_Note: errored DB trades may still have filled on Delta (cancel/rollback bugs). "
        "MAE/MFE uses underlying 15m bars only, not option marks._"
    )
    return "\n".join(lines)


async def run_postmortem(
    db_path: Path,
    *,
    since: str | None,
    until: str | None,
    mode: str,
    status: str | None,
    dedupe: bool,
    max_samples: int,
    output: Path | None,
) -> str:
    since_dt = dt.datetime.fromisoformat(since) if since else None
    until_dt = dt.datetime.fromisoformat(until) if until else None
    report = await build_report(
        db_path,
        since=since_dt,
        until=until_dt,
        mode=mode,
        status=status,
        dedupe=dedupe,
        max_samples=max_samples,
    )
    text = format_report(report)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    return text
