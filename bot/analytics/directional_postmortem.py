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


@dataclass
class OpenPositionRow:
    product_symbol: str
    size: float
    exchange_entry_price: float | None
    unrealized_pnl: float | None
    trade_id: int | None
    trade_status: str | None
    trade_error: str | None
    entry_ts: dt.datetime | None
    underlying: str | None
    option_type: str | None
    signal_spot: float | None
    signal_atr: float | None
    entry_reason: str
    execution_note: str
    current_spot: float | None
    adverse_move: float | None
    underlying_sl_atr: float | None
    underlying_sl_room: float | None
    at_underlying_sl: bool
    option_mark: float | None
    premium_dd_pct: float | None
    at_premium_sl: bool
    exchange_orders: list[dict[str, Any]]


@dataclass
class SkippedPosition:
    """Open Delta position that is not a directional BTC/ETH option."""

    product_symbol: str
    size: float
    entry_price: float | None
    unrealized_pnl: float | None


def _is_bot_option_symbol(product_symbol: str) -> bool:
    """True for Delta India option symbols the directional strategy trades (C/P-BTC|ETH-...)."""
    parts = product_symbol.split("-")
    if len(parts) < 4:
        return False
    prefix, und = parts[0], parts[1]
    return prefix in ("C", "P") and und in ("BTC", "ETH")


def find_trade_for_symbol(db_path: Path, product_symbol: str) -> dict[str, Any] | None:
    """Best-match directional trade row for an open Delta contract symbol."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT t.id, t.entry_ts, t.status, t.mode, t.notes,
               s.intended_symbol, s.intended_strike, s.intended_premium_inr, s.feature_vector
        FROM trades t
        LEFT JOIN signals s ON s.id = t.signal_id
        WHERE t.strategy_id = 'directional'
          AND s.intended_symbol = ?
        ORDER BY t.entry_ts DESC
        LIMIT 1
        """,
        (product_symbol,),
    ).fetchall()
    if not rows:
        # Strike-level fallback: C-BTC-73800-290526 vs near matches
        parts = product_symbol.split("-")
        if len(parts) >= 3:
            und, strike = parts[1], parts[2]
            like = f"%-{und}-{strike}-%"
            rows = conn.execute(
                """
                SELECT t.id, t.entry_ts, t.status, t.mode, t.notes,
                       s.intended_symbol, s.intended_strike, s.intended_premium_inr, s.feature_vector
                FROM trades t
                LEFT JOIN signals s ON s.id = t.signal_id
                WHERE t.strategy_id = 'directional'
                  AND s.intended_symbol LIKE ?
                ORDER BY t.entry_ts DESC
                LIMIT 1
                """,
                (like,),
            ).fetchall()
    conn.close()
    return dict(rows[0]) if rows else None


def _explain_entry_from_features(fv: dict[str, Any], option_type: str | None) -> str:
    ema_sep = _as_float(fv.get("ema_sep"))
    threshold = _as_float(fv.get("threshold"))
    atr_v = _as_float(fv.get("atr"))
    close = _as_float(fv.get("latest_close"))
    prior_high = _as_float(fv.get("prior_high"))
    prior_low = _as_float(fv.get("prior_low"))
    long_setup = fv.get("long_setup")
    short_setup = fv.get("short_setup")
    if long_setup is None and ema_sep is not None and threshold is not None:
        long_setup = ema_sep > threshold
    if short_setup is None and ema_sep is not None and threshold is not None:
        short_setup = ema_sep < -threshold
    parts: list[str] = []
    if option_type == "call" or long_setup:
        parts.append(
            "**Long breakout (call):** 15m EMA9>EMA21 by >= breakout x ATR and close above 4-bar high + threshold."
        )
    elif option_type == "put" or short_setup:
        parts.append(
            "**Short breakout (put):** 15m EMA9<EMA21 by >= breakout x ATR and close below 4-bar low - threshold."
        )
    if ema_sep is not None and threshold is not None:
        parts.append(f"At signal: ema_sep={ema_sep:.2f}, threshold={threshold:.2f} (~0.35 x ATR).")
    if atr_v is not None:
        parts.append(f"ATR(14)={atr_v:.2f}.")
    if close is not None and prior_high is not None and prior_low is not None:
        parts.append(f"Close={close:.2f}, prior_high={prior_high:.2f}, prior_low={prior_low:.2f}.")
    iv_p = fv.get("iv_percentile")
    if iv_p is not None:
        parts.append(f"IV percentile={iv_p}.")
    return " ".join(parts) if parts else "No feature_vector stored on signal."


def _underlying_sl_metrics(
    option_type: str,
    entry_spot: float,
    entry_atr: float,
    spot_now: float,
    atr_mult: float,
) -> tuple[float, float, float, bool]:
    """adverse_move, sl_threshold, room_until_sl (negative = past SL), at_sl."""
    threshold = atr_mult * entry_atr
    adverse = entry_spot - spot_now if option_type == "call" else spot_now - entry_spot
    room = threshold - adverse
    return adverse, threshold, room, adverse >= threshold


async def build_open_positions_report(
    db_path: Path,
    *,
    config_dir: Path,
    premium_dd_pct: float = 0.50,
    underlying_atr_mult: float = 1.0,
    options_only: bool = True,
) -> tuple[list[OpenPositionRow], list[SkippedPosition], list[str]]:
    from bot.config.loader import load_strategy_configs
    from bot.config.models import DirectionalConfig, StrategyId

    for cfg in load_strategy_configs(config_dir):
        if cfg.id == StrategyId.DIRECTIONAL and isinstance(cfg, DirectionalConfig):
            premium_dd_pct = cfg.exits.premium_drawdown_pct
            underlying_atr_mult = cfg.exits.underlying_atr_mult_stop
            break

    settings = Settings()
    rows: list[OpenPositionRow] = []
    errors: list[str] = []

    async with DeltaRestClient(settings) as rest:
        try:
            positions = await rest.get_positions()
        except DeltaRestError as exc:
            return [], [], [f"get_positions: {exc}"]

        open_pos = [p for p in positions if float(p.get("size") or 0) != 0]
        if not open_pos:
            return [], [], []

        skipped: list[SkippedPosition] = []
        for p in open_pos:
            sym = str(p.get("product_symbol") or "")
            if options_only and not _is_bot_option_symbol(sym):
                skipped.append(
                    SkippedPosition(
                        product_symbol=sym,
                        size=float(p.get("size") or 0),
                        entry_price=_as_float(p.get("entry_price")),
                        unrealized_pnl=_as_float(p.get("unrealized_pnl")),
                    )
                )

        for p in open_pos:
            sym = str(p.get("product_symbol") or "")
            if options_only and not _is_bot_option_symbol(sym):
                continue
            size = float(p.get("size") or 0)
            ex_entry = _as_float(p.get("entry_price"))
            upnl = _as_float(p.get("unrealized_pnl"))
            opt = _option_type_from_symbol(sym)
            und = "BTC" if "BTC" in sym else "ETH" if "ETH" in sym else None

            trade_row = find_trade_for_symbol(db_path, sym) if sym else None
            trade_id: int | None = None
            trade_status: str | None = None
            trade_error: str | None = None
            entry_ts: dt.datetime | None = None
            fv: dict[str, Any] = {}
            signal_spot: float | None = None
            signal_atr: float | None = None
            if trade_row:
                trade_id = int(trade_row["id"])
                trade_status = str(trade_row["status"])
                entry_ts = _parse_ts(str(trade_row["entry_ts"]))
                notes_raw = trade_row.get("notes")
                if notes_raw:
                    try:
                        notes = json.loads(notes_raw) if isinstance(notes_raw, str) else dict(notes_raw)
                        trade_error = notes.get("error") if isinstance(notes.get("error"), str) else None
                    except (TypeError, json.JSONDecodeError):
                        pass
                fv_raw = trade_row.get("feature_vector")
                if fv_raw:
                    try:
                        fv = json.loads(fv_raw) if isinstance(fv_raw, str) else dict(fv_raw)
                    except (TypeError, json.JSONDecodeError):
                        fv = {}
                signal_spot = _as_float(fv.get("spot"))
                signal_atr = _as_float(fv.get("atr"))

            entry_reason = _explain_entry_from_features(fv, opt)
            if trade_status == "errored":
                execution_note = (
                    "DB trade is **errored** (usually `partial_fill_rolled_back`): bot is **not** "
                    "running `manage()` stops on this position. You must close on Delta or adopt it manually."
                )
            elif trade_status == "open":
                execution_note = "DB trade is **open** — bot should manage stops if the process is healthy."
            else:
                execution_note = (
                    "No matching DB trade or status is not open — position may be orphan / untracked."
                )

            current_spot: float | None = None
            u_sym = UNDERLYING_CANDLE_SYMBOL.get(und or "")
            if u_sym:
                try:
                    ticker = await rest.get_ticker(u_sym)
                    current_spot = _as_float(ticker.get("mark_price") or ticker.get("spot_price"))
                except DeltaRestError as exc:
                    errors.append(f"ticker {u_sym}: {exc}")

            adverse = sl_atr = room = None
            at_und_sl = False
            if (
                opt in ("call", "put")
                and signal_spot is not None
                and signal_atr is not None
                and current_spot is not None
            ):
                adverse, sl_atr, room, at_und_sl = _underlying_sl_metrics(
                    opt, signal_spot, signal_atr, current_spot, underlying_atr_mult
                )

            option_mark: float | None = None
            premium_dd: float | None = None
            at_prem_sl = False
            if sym:
                try:
                    ot = await rest.get_ticker(sym)
                    option_mark = _as_float(ot.get("mark_price") or ot.get("close"))
                except DeltaRestError as exc:
                    errors.append(f"option ticker {sym}: {exc}")
            prem_entry = ex_entry
            if prem_entry is not None and prem_entry > 0 and option_mark is not None:
                premium_dd = (prem_entry - option_mark) / prem_entry
                at_prem_sl = option_mark <= prem_entry * (1.0 - premium_dd_pct)

            orders: list[dict[str, Any]] = []
            if trade_id is not None:
                try:
                    orders = await fetch_delta_orders_for_trade(rest, trade_id)
                except DeltaRestError as exc:
                    errors.append(f"orders trade {trade_id}: {exc}")

            rows.append(
                OpenPositionRow(
                    product_symbol=sym,
                    size=size,
                    exchange_entry_price=ex_entry,
                    unrealized_pnl=upnl,
                    trade_id=trade_id,
                    trade_status=trade_status,
                    trade_error=trade_error,
                    entry_ts=entry_ts,
                    underlying=und,
                    option_type=opt,
                    signal_spot=signal_spot,
                    signal_atr=signal_atr,
                    entry_reason=entry_reason,
                    execution_note=execution_note,
                    current_spot=current_spot,
                    adverse_move=adverse,
                    underlying_sl_atr=sl_atr,
                    underlying_sl_room=room,
                    at_underlying_sl=at_und_sl,
                    option_mark=option_mark,
                    premium_dd_pct=premium_dd * 100.0 if premium_dd is not None else None,
                    at_premium_sl=at_prem_sl,
                    exchange_orders=orders,
                )
            )

    return rows, skipped, errors


def format_open_positions_report(
    rows: list[OpenPositionRow],
    skipped: list[SkippedPosition],
    errors: list[str],
    *,
    premium_dd_pct: float = 0.50,
    underlying_atr_mult: float = 1.0,
    options_only: bool = True,
) -> str:
    lines = [
        "# Open Delta positions — why entered & stop proximity",
        "",
        "Links live exchange size to SQLite signal + configured directional stops. "
        "**Errored** trades are not managed by the bot.",
        "",
        f"Config refs: `underlying_atr_mult_stop={underlying_atr_mult}`, "
        f"`premium_drawdown_pct={premium_dd_pct}`.",
        "",
    ]
    if not rows:
        if options_only:
            lines.append(
                "No open **BTC/ETH option** positions on Delta (bot scope: `C-*` / `P-*` on BTC or ETH)."
            )
        else:
            lines.append("No open positions (size != 0) on Delta.")
        lines.append("")
    for r in rows:
        lines.append(f"## {r.product_symbol} (size={r.size})")
        lines.append("")
        if r.trade_id is not None:
            lines.append(
                f"- **DB trade** `{r.trade_id}` | status=`{r.trade_status}` | "
                f"error=`{r.trade_error or '-'}` | entry={r.entry_ts}"
            )
        else:
            lines.append("- **DB trade:** no matching `signals.intended_symbol` row")
        lines.append(f"- **Exchange:** entry_price={r.exchange_entry_price} uPnL={r.unrealized_pnl}")
        lines.append("")
        lines.append("### Why the bot entered")
        lines.append("")
        lines.append(r.entry_reason)
        lines.append("")
        lines.append("### Execution / why stops may not fire")
        lines.append("")
        lines.append(r.execution_note)
        lines.append("")
        lines.append("### Stop proximity (bot rules, approximate)")
        lines.append("")
        if r.signal_spot is not None and r.current_spot is not None:
            lines.append(
                f"- Spot at signal: **{r.signal_spot:.2f}** → now **{r.current_spot:.2f}** ({r.underlying})"
            )
        if r.adverse_move is not None and r.underlying_sl_atr is not None:
            status = "**AT/ PAST underlying stop**" if r.at_underlying_sl else "not yet at underlying stop"
            room_s = f"{r.underlying_sl_room:.2f}" if r.underlying_sl_room is not None else "?"
            lines.append(
                f"- **Underlying stop** ({r.option_type}): adverse move **{r.adverse_move:.2f}** "
                f"vs limit **{r.underlying_sl_atr:.2f}** (1 x ATR at entry). Room: **{room_s}** - {status}."
            )
        if r.option_mark is not None and r.exchange_entry_price is not None:
            dd_s = f"{r.premium_dd_pct:.1f}%" if r.premium_dd_pct is not None else "?"
            prem_status = "**AT/ PAST premium stop**" if r.at_premium_sl else "not yet at premium stop"
            lines.append(
                f"- **Premium stop** (50% DD on option mark): entry **{r.exchange_entry_price}** "
                f"mark **{r.option_mark}** DD **{dd_s}** - {prem_status}."
            )
        lines.append("")
        if r.exchange_orders:
            lines.append("### Exchange orders (`dr-*`)")
            for o in r.exchange_orders:
                lines.append(
                    f"- `{o.get('client_order_id')}` state={o.get('state')} "
                    f"filled={o.get('filled_size')} avg={o.get('average_fill_price')}"
                )
            lines.append("")
        lines.append("---")
        lines.append("")

    if skipped:
        lines.append("## Other open positions (not directional BTC/ETH options)")
        lines.append("")
        lines.append(
            "These are on the same Delta account but **not** from the options bot. "
            "Manage in Delta UI; they are ignored for entry/stop analysis below."
        )
        lines.append("")
        for s in skipped:
            lines.append(
                f"- **{s.product_symbol}**: size={s.size} entry={s.entry_price} uPnL={s.unrealized_pnl}"
            )
        lines.append("")

    if errors:
        lines.append("## Warnings")
        for e in errors[:15]:
            lines.append(f"- {e}")
    return "\n".join(lines)


async def run_open_positions_analysis(
    db_path: Path,
    *,
    config_dir: Path,
    output: Path | None,
    options_only: bool = True,
) -> str:
    from bot.config.loader import load_strategy_configs
    from bot.config.models import DirectionalConfig, StrategyId

    prem_dd = 0.50
    und_mult = 1.0
    for cfg in load_strategy_configs(config_dir):
        if cfg.id == StrategyId.DIRECTIONAL and isinstance(cfg, DirectionalConfig):
            prem_dd = cfg.exits.premium_drawdown_pct
            und_mult = cfg.exits.underlying_atr_mult_stop
            break

    rows, skipped, errors = await build_open_positions_report(
        db_path,
        config_dir=config_dir,
        premium_dd_pct=prem_dd,
        underlying_atr_mult=und_mult,
        options_only=options_only,
    )
    text = format_open_positions_report(
        rows,
        skipped,
        errors,
        premium_dd_pct=prem_dd,
        underlying_atr_mult=und_mult,
        options_only=options_only,
    )
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    return text


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
