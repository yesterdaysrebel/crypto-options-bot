"""Optimization study: filled directional trades vs underlying + option price paths.

Focuses on trades that actually executed (closed/open with leg fills), not errored DB rows.
Pulls Delta 15m candles to relate exit reasons (premium_stop, underlying_stop, target) to
spot/option movement and suggests concrete parameter tweaks.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot.analytics.directional_postmortem import (
    UNDERLYING_CANDLE_SYMBOL,
    _as_float,
    _fetch_candles,
    _nearest_candle_index,
    _option_type_from_symbol,
    _parse_ts,
    _spot_favorable,
)
from bot.config.settings import Settings
from bot.exchange.rest import DeltaRestClient, DeltaRestError

# Match config/strategies/directional.yaml defaults (override via CLI later if needed).
PREMIUM_DD_PCT = 0.50
UNDERLYING_ATR_STOP_MULT = 1.0
TARGET_R = 1.5

_DR_COID = re.compile(r"^dr-(\d+)-\d+-")


@dataclass
class FilledTrade:
    trade_id: int
    entry_ts: dt.datetime
    exit_ts: dt.datetime | None
    underlying: str
    mode: str
    status: str
    symbol: str
    option_type: str | None
    strike: float | None
    lots: int
    entry_premium: float | None  # per lot INR (leg.entry_price)
    exit_premium: float | None
    realised_pnl_inr: float | None
    r_multiple: float | None
    exit_reason: str | None
    spot_at_entry: float | None
    atr_at_entry: float | None
    ema_sep: float | None
    threshold: float | None
    source: str  # db | exchange


@dataclass
class TradePathAnalysis:
    trade: FilledTrade
    hold_minutes: float
    spot_ret_at_exit_pct: float | None
    spot_adverse_15m: bool | None
    spot_verdict_60m: str
    premium_dd_pct_max: float | None  # option candle proxy: max drawdown vs entry bar
    premium_dd_hit_first_bar: int | None  # 15m bars after entry when DD >= 50%
    underlying_sl_hit_first_bar: int | None
    win: bool


@dataclass
class OptimizationReport:
    trades: list[FilledTrade]
    paths: list[TradePathAnalysis]
    exchange_orphans: list[dict[str, Any]]
    errors: list[str]


def load_filled_trades_from_db(
    db_path: Path,
    *,
    since: dt.datetime | None,
    until: dt.datetime | None,
    modes: tuple[str, ...] = ("dry", "live"),
) -> list[FilledTrade]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    clauses = [
        "t.strategy_id = 'directional'",
        "t.status IN ('closed', 'open')",
        "l.entry_price IS NOT NULL",
    ]
    params: list[Any] = []
    if since is not None:
        clauses.append("t.entry_ts >= ?")
        params.append(since.isoformat(sep=" "))
    if until is not None:
        clauses.append("t.entry_ts < ?")
        params.append(until.isoformat(sep=" "))
    if modes:
        placeholders = ",".join("?" for _ in modes)
        clauses.append(f"t.mode IN ({placeholders})")
        params.extend(modes)
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"""
        SELECT t.id, t.entry_ts, t.exit_ts, t.underlying, t.mode, t.status, t.lots,
               t.premium_paid_inr, t.realised_pnl_inr, t.r_multiple, t.exit_reason, t.notes,
               s.feature_vector,
               l.symbol, l.option_type, l.strike, l.entry_price, l.exit_price
        FROM trades t
        JOIN legs l ON l.trade_id = t.id AND l.leg_idx = 0
        LEFT JOIN signals s ON s.id = t.signal_id
        WHERE {where}
        ORDER BY t.entry_ts ASC
        """,
        params,
    ).fetchall()
    conn.close()
    out: list[FilledTrade] = []
    for r in rows:
        fv: dict[str, Any] = {}
        if r["feature_vector"]:
            try:
                fv = (
                    json.loads(r["feature_vector"])
                    if isinstance(r["feature_vector"], str)
                    else dict(r["feature_vector"])
                )
            except (TypeError, json.JSONDecodeError):
                fv = {}
        sym = str(r["symbol"])
        out.append(
            FilledTrade(
                trade_id=int(r["id"]),
                entry_ts=_parse_ts(str(r["entry_ts"])),
                exit_ts=_parse_ts(str(r["exit_ts"])) if r["exit_ts"] else None,
                underlying=str(r["underlying"]),
                mode=str(r["mode"]),
                status=str(r["status"]),
                symbol=sym,
                option_type=_option_type_from_symbol(sym) or r["option_type"],
                strike=_as_float(r["strike"]),
                lots=int(r["lots"] or 1),
                entry_premium=_as_float(r["entry_price"]) or _as_float(r["premium_paid_inr"]),
                exit_premium=_as_float(r["exit_price"]),
                realised_pnl_inr=_as_float(r["realised_pnl_inr"]),
                r_multiple=_as_float(r["r_multiple"]),
                exit_reason=str(r["exit_reason"]) if r["exit_reason"] else None,
                spot_at_entry=_as_float(fv.get("spot")),
                atr_at_entry=_as_float(fv.get("atr")),
                ema_sep=_as_float(fv.get("ema_sep")),
                threshold=_as_float(fv.get("threshold")),
                source="db",
            )
        )
    return out


def _trade_id_from_coid(coid: str) -> int | None:
    m = _DR_COID.match(coid)
    if not m:
        return None
    return int(m.group(1))


async def load_filled_trades_from_exchange(
    rest: DeltaRestClient,
    *,
    since: dt.datetime,
) -> tuple[list[FilledTrade], list[dict[str, Any]]]:
    """Build pseudo-trades from filled Delta orders (for live fills missing in DB)."""
    by_tid: dict[int, list[dict[str, Any]]] = defaultdict(list)
    orphans: list[dict[str, Any]] = []
    for state in ("closed", "open"):
        try:
            env = await rest._request("GET", "/v2/orders", params={"state": state}, signed=True)
        except DeltaRestError:
            continue
        rows = env.get("result") or []
        if isinstance(rows, dict):
            rows = [rows]
        for o in rows:
            if not isinstance(o, dict):
                continue
            coid = str(o.get("client_order_id") or "")
            tid = _trade_id_from_coid(coid)
            if tid is None:
                continue
            created = o.get("created_at") or o.get("updated_at")
            if created is None:
                continue
            # created_at may be microsecond int
            if isinstance(created, (int, float)):
                ts = dt.datetime.fromtimestamp(
                    int(created) / 1_000_000 if created > 1e12 else int(created), tz=dt.UTC
                ).replace(tzinfo=None)
            else:
                ts = _parse_ts(str(created))
            if ts < since:
                continue
            filled = float(o.get("filled_size") or 0)
            if filled <= 0:
                continue
            by_tid[tid].append(o)

    out: list[FilledTrade] = []
    for tid, orders in sorted(by_tid.items()):
        orders_sorted = sorted(orders, key=lambda x: str(x.get("created_at") or ""))
        entry_o = None
        exit_o = None
        for o in orders_sorted:
            coid = str(o.get("client_order_id") or "")
            if (
                entry_o is None
                and "exit" not in coid
                and "trail" not in coid
                and ("entry" in coid or "rollback" not in coid)
            ):
                entry_o = o
            if "exit" in coid or "rollback" in coid:
                exit_o = o
        if entry_o is None:
            orphans.extend(orders_sorted)
            continue
        sym = str(entry_o.get("product_symbol") or "")
        und = "BTC" if "BTC" in sym else "ETH" if "ETH" in sym else "?"
        created = entry_o.get("created_at")
        if isinstance(created, (int, float)):
            entry_ts = dt.datetime.fromtimestamp(
                int(created) / 1_000_000 if created > 1e12 else int(created), tz=dt.UTC
            ).replace(tzinfo=None)
        else:
            entry_ts = _parse_ts(str(created))
        exit_ts = None
        if exit_o is not None:
            ec = exit_o.get("created_at")
            if isinstance(ec, (int, float)):
                exit_ts = dt.datetime.fromtimestamp(
                    int(ec) / 1_000_000 if ec > 1e12 else int(ec), tz=dt.UTC
                ).replace(tzinfo=None)
            else:
                exit_ts = _parse_ts(str(ec))
        out.append(
            FilledTrade(
                trade_id=tid,
                entry_ts=entry_ts,
                exit_ts=exit_ts,
                underlying=und,
                mode="live",
                status="closed" if exit_o else "open",
                symbol=sym,
                option_type=_option_type_from_symbol(sym),
                strike=None,
                lots=int(float(entry_o.get("size") or 1)),
                entry_premium=_as_float(entry_o.get("average_fill_price")),
                exit_premium=_as_float(exit_o.get("average_fill_price")) if exit_o else None,
                realised_pnl_inr=None,
                r_multiple=None,
                exit_reason="exchange_only",
                spot_at_entry=None,
                atr_at_entry=None,
                ema_sep=None,
                threshold=None,
                source="exchange",
            )
        )
    return out, orphans


def _premium_dd_from_candles(
    candles: list[dict[str, Any]],
    entry_idx: int,
    *,
    dd_threshold: float = PREMIUM_DD_PCT,
) -> tuple[float | None, int | None]:
    if entry_idx >= len(candles):
        return None, None
    entry_close = float(candles[entry_idx].get("close") or 0)
    if entry_close <= 0:
        return None, None
    max_dd = 0.0
    first_hit: int | None = None
    for j in range(entry_idx + 1, len(candles)):
        close = float(candles[j].get("close") or entry_close)
        dd = 1.0 - (close / entry_close)
        max_dd = max(max_dd, dd)
        bars_fwd = j - entry_idx
        if first_hit is None and dd >= dd_threshold:
            first_hit = bars_fwd
    return max_dd * 100.0, first_hit


def _underlying_sl_bar(
    candles: list[dict[str, Any]],
    entry_idx: int,
    *,
    option_type: str | None,
    entry_spot: float,
    atr: float | None,
) -> int | None:
    if atr is None or atr <= 0 or entry_idx >= len(candles):
        return None
    limit = UNDERLYING_ATR_STOP_MULT * atr
    for j in range(entry_idx + 1, len(candles)):
        high = float(candles[j].get("high") or 0)
        low = float(candles[j].get("low") or 0)
        if option_type == "call":
            adverse = entry_spot - low
        elif option_type == "put":
            adverse = high - entry_spot
        else:
            continue
        if adverse >= limit:
            return j - entry_idx
    return None


def analyze_trade_path(
    trade: FilledTrade,
    underlying_candles: list[dict[str, Any]],
    option_candles: list[dict[str, Any]],
) -> TradePathAnalysis | None:
    entry_spot = trade.spot_at_entry
    u_idx = _nearest_candle_index(underlying_candles, trade.entry_ts)
    if u_idx is None:
        return None
    if entry_spot is None or entry_spot <= 0:
        entry_spot = float(underlying_candles[u_idx].get("close") or 0)
    if entry_spot <= 0:
        return None

    end_ts = trade.exit_ts or (trade.entry_ts + dt.timedelta(hours=4))
    hold_minutes = (end_ts - trade.entry_ts).total_seconds() / 60.0

    exit_idx = _nearest_candle_index(underlying_candles, end_ts) if trade.exit_ts else u_idx
    spot_ret_exit = None
    if exit_idx is not None and exit_idx > u_idx:
        exit_close = float(underlying_candles[exit_idx].get("close") or entry_spot)
        spot_ret_exit = (exit_close - entry_spot) / entry_spot

    r15 = None
    if u_idx + 1 < len(underlying_candles):
        c15 = float(underlying_candles[u_idx + 1].get("close") or entry_spot)
        r15 = (c15 - entry_spot) / entry_spot
    spot_adverse_15m = None
    if r15 is not None:
        verdict15 = _spot_favorable(trade.option_type, r15)
        spot_adverse_15m = verdict15 == "adverse"

    r60 = None
    if u_idx + 4 < len(underlying_candles):
        c60 = float(underlying_candles[u_idx + 4].get("close") or entry_spot)
        r60 = (c60 - entry_spot) / entry_spot
    spot_verdict_60m = _spot_favorable(trade.option_type, r60 if r60 is not None else 0.0)

    o_idx = _nearest_candle_index(option_candles, trade.entry_ts) if option_candles else None
    prem_dd_max, prem_dd_bar = None, None
    und_sl_bar = _underlying_sl_bar(
        underlying_candles,
        u_idx,
        option_type=trade.option_type,
        entry_spot=entry_spot,
        atr=trade.atr_at_entry,
    )
    if o_idx is not None:
        prem_dd_max, prem_dd_bar = _premium_dd_from_candles(option_candles, o_idx)

    win = trade.realised_pnl_inr is not None and trade.realised_pnl_inr > 0
    if trade.realised_pnl_inr is None and trade.entry_premium and trade.exit_premium:
        win = trade.exit_premium > trade.entry_premium

    return TradePathAnalysis(
        trade=trade,
        hold_minutes=hold_minutes,
        spot_ret_at_exit_pct=spot_ret_exit * 100.0 if spot_ret_exit is not None else None,
        spot_adverse_15m=spot_adverse_15m,
        premium_dd_pct_max=prem_dd_max,
        premium_dd_hit_first_bar=prem_dd_bar,
        underlying_sl_hit_first_bar=und_sl_bar,
        spot_verdict_60m=spot_verdict_60m,
        win=win,
    )


async def build_optimization_report(
    db_path: Path,
    *,
    since: dt.datetime | None,
    until: dt.datetime | None,
    include_exchange: bool = True,
) -> OptimizationReport:
    trades = load_filled_trades_from_db(db_path, since=since, until=until)
    paths: list[TradePathAnalysis] = []
    errors: list[str] = []
    orphans: list[dict[str, Any]] = []
    settings = Settings()
    u_cache: dict[str, list[dict[str, Any]]] = {}
    o_cache: dict[str, list[dict[str, Any]]] = {}

    async with DeltaRestClient(settings) as rest:
        if include_exchange:
            ex_since = since or (dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(days=14))
            try:
                ex_trades, orphans = await load_filled_trades_from_exchange(rest, since=ex_since)
                known = {t.trade_id for t in trades}
                for et in ex_trades:
                    if et.trade_id not in known:
                        trades.append(et)
            except DeltaRestError as exc:
                errors.append(f"exchange orders: {exc}")

        for trade in trades:
            u_sym = UNDERLYING_CANDLE_SYMBOL.get(trade.underlying)
            if not u_sym:
                errors.append(f"trade {trade.trade_id}: bad underlying")
                continue
            end = trade.exit_ts or trade.entry_ts + dt.timedelta(hours=6)
            start = trade.entry_ts - dt.timedelta(hours=1)
            u_key = f"{u_sym}:{start.date()}:{end.date()}"
            if u_key not in u_cache:
                try:
                    u_cache[u_key] = await _fetch_candles(rest, u_sym, start, end)
                except DeltaRestError as exc:
                    errors.append(f"underlying candles {u_sym}: {exc}")
                    u_cache[u_key] = []
            o_candles: list[dict[str, Any]] = []
            if trade.symbol:
                o_key = f"{trade.symbol}:{start.date()}:{end.date()}"
                if o_key not in o_cache:
                    try:
                        o_cache[o_key] = await _fetch_candles(rest, trade.symbol, start, end)
                    except DeltaRestError as exc:
                        errors.append(f"option candles {trade.symbol}: {exc}")
                        o_cache[o_key] = []
                o_candles = o_cache[o_key]
            row = analyze_trade_path(trade, u_cache[u_key], o_candles)
            if row is not None:
                paths.append(row)

    return OptimizationReport(trades=trades, paths=paths, exchange_orphans=orphans, errors=errors)


def _suggestions(paths: list[TradePathAnalysis]) -> list[str]:
    if not paths:
        return ["No filled trades with price paths — run after dry/live round-trips exist in DB or on Delta."]
    n = len(paths)
    losses = [p for p in paths if not p.win]
    sl_premium = sum(1 for p in paths if (p.trade.exit_reason or "") == "premium_stop")
    sl_und = sum(1 for p in paths if (p.trade.exit_reason or "") == "underlying_stop")
    adverse_15 = sum(1 for p in paths if p.spot_adverse_15m is True)
    suggestions: list[str] = []

    if adverse_15 >= max(1, int(0.6 * n)):
        suggestions.append(
            f"**Entry timing:** {adverse_15}/{n} trades saw spot move against the thesis within 15m. "
            "Consider raising `entry.breakout_atr_mult` (now 0.35), or require 1h EMA alignment before entry."
        )
    if sl_premium >= sl_und and sl_premium >= n // 3:
        suggestions.append(
            f"**Premium stop:** {sl_premium}/{n} exits were `premium_stop` (50% option drawdown). "
            "Either entries are late/choppy, or `premium_drawdown_pct` is tight for 15m breakouts — "
            "test 0.55-0.60 or add spread/IV filter at entry."
        )
    if sl_und > sl_premium:
        suggestions.append(
            f"**Underlying stop:** {sl_und}/{n} hit `underlying_stop` (1.0x ATR). "
            "Widen `underlying_atr_mult_stop` to 1.25-1.5 if whipsaw stops dominate, "
            "or tighten breakout so entries aren't at exhaustion moves."
        )
    early_prem = [
        p for p in paths if p.premium_dd_hit_first_bar is not None and p.premium_dd_hit_first_bar <= 2
    ]
    if len(early_prem) >= max(1, int(0.5 * len(losses))):
        suggestions.append(
            f"**Fast drawdown:** {len(early_prem)} trades hit 50% premium DD within 30m on option candles. "
            "Reduce size (`max_lots_cap` / risk%) or skip entries when `spread_pct` is elevated."
        )
    if not suggestions:
        suggestions.append(
            "Sample is small or mixed; collect more closed trades before large parameter changes."
        )
    return suggestions


def format_optimization_report(report: OptimizationReport) -> str:
    lines = [
        "# Directional optimization report (filled trades + Delta prices)",
        "",
        "Trades with actual fills only (closed/open). Errored rows excluded.",
        "",
        f"- Filled trades loaded: **{len(report.trades)}**",
        f"- Price paths analyzed: **{len(report.paths)}**",
        "",
    ]

    if report.trades:
        by_exit: dict[str, int] = defaultdict(int)
        wins = 0
        for t in report.trades:
            by_exit[t.exit_reason or "unknown"] += 1
            if t.realised_pnl_inr is not None and t.realised_pnl_inr > 0:
                wins += 1
        lines.append("## Trade outcomes (DB + exchange)")
        lines.append("")
        lines.append(f"- Wins (realised_pnl > 0): {wins} / {len(report.trades)}")
        for reason, cnt in sorted(by_exit.items(), key=lambda x: -x[1]):
            lines.append(f"- `{reason}`: {cnt}")
        lines.append("")

    if report.paths:
        lines.append("## Price path summary")
        lines.append("")
        adv = sum(1 for p in report.paths if p.spot_adverse_15m)
        lines.append(f"- Spot adverse within 15m: {adv} / {len(report.paths)}")
        lines.append("")
        lines.append(
            "| id | src | mode | exit | opt | hold m | entry prem | PnL | spot@exit% | "
            "adverse@15m | opt DD% max | prem SL bar | und SL bar | 60m spot |"
        )
        lines.append(
            "|---:|-----|------|------|-----|-------:|-----------:|----:|-----------:|"
            "------------:|------------:|------------:|-----------:|----------|"
        )
        for p in report.paths:
            t = p.trade
            lines.append(
                f"| {t.trade_id} | {t.source} | {t.mode} | {t.exit_reason or ''} | {t.option_type or ''} | "
                f"{p.hold_minutes:.0f} | {t.entry_premium or ''} | {t.realised_pnl_inr or ''} | "
                f"{p.spot_ret_at_exit_pct if p.spot_ret_at_exit_pct is not None else ''} | "
                f"{p.spot_adverse_15m} | {p.premium_dd_pct_max if p.premium_dd_pct_max is not None else ''} | "
                f"{p.premium_dd_hit_first_bar or ''} | {p.underlying_sl_hit_first_bar or ''} | "
                f"{p.spot_verdict_60m} |"
            )
        lines.append("")

    lines.append("## Suggested refinements")
    lines.append("")
    for s in _suggestions(report.paths):
        lines.append(f"- {s}")
    lines.append("")

    if report.exchange_orphans:
        lines.append(f"## Exchange orders without clear entry ({len(report.exchange_orphans)} orphan legs)")
        lines.append("")

    if report.errors:
        lines.append("## Warnings")
        for e in report.errors[:15]:
            lines.append(f"- {e}")

    lines.append("")
    lines.append(
        "_Option DD uses 15m candle closes on the contract symbol (proxy for premium). "
        "Compare with Delta UI marks for live trades not fully synced to DB._"
    )
    return "\n".join(lines)


async def run_optimization(
    db_path: Path,
    *,
    since: str | None,
    until: str | None,
    output: Path | None,
) -> str:
    since_dt = dt.datetime.fromisoformat(since) if since else None
    until_dt = dt.datetime.fromisoformat(until) if until else None
    report = await build_optimization_report(db_path, since=since_dt, until=until_dt)
    text = format_optimization_report(report)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    return text
