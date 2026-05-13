"""Live trading engine: Delta REST/WS, chain cache, strategy dispatch, dry execution, analytics."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import signal
import time
from collections import defaultdict
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.analytics.daily import DailyAggregator
from bot.analytics.decision_log import DecisionLogWriter
from bot.analytics.journal import TradeJournal
from bot.config.loader import load_all
from bot.config.models import (
    DirectionalConfig,
    IronCondorConfig,
    StrategyConfig,
    StrategyId,
    Underlying,
    VolStrangleConfig,
)
from bot.data.candles import CandleAggregator
from bot.data.chain_cache import ChainCache, QuoteSnapshot, parse_symbol
from bot.exchange.rest import DeltaRestClient, DeltaRestError
from bot.exchange.wallet import fetch_wallet_snapshot
from bot.exchange.ws import DeltaWebSocketClient, Subscription
from bot.execution.dry import DryExecutor
from bot.execution.router import EntryRequest, ExitRequest, LegSide
from bot.observability.logging_setup import configure_logging
from bot.observability.metrics import MetricsRegistry, TextfileCollector
from bot.observability.server import MetricsServer
from bot.risk.caps import NavTracker
from bot.risk.manager import RiskDecision, RiskManager, SizingResult, TradeAccountingSnapshot
from bot.runtime.trade_tracking import (
    indicator_snapshot_for_trade,
    indicator_snapshot_for_underlying,
    refresh_all_open_trades,
)
from bot.storage.db import Database, get_database
from bot.storage.models import (
    DecisionKind,
    DecisionReason,
    Leg,
    LegStatus,
    NavHistory,
    Order,
    OrderState,
    OrderType,
    Signal,
    Trade,
    TradeStatus,
)
from bot.strategies import (
    DirectionalStrategy,
    IronCondorStrategy,
    MarketState,
    PositionState,
    Strategy,
    StrategyDispatcher,
    StrategyRegistry,
    VolStrangleStrategy,
)
from bot.strategies.base import ActionType, ExitTrigger, Intent, LegIntent, TrailAction

WALLET_POLL_INTERVAL_S = 30.0
OPEN_JOURNAL_REFRESH_S = 30.0


def _note_float(notes: dict[str, Any], key: str) -> float | None:
    v = notes.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _build_strategies(configs: list[StrategyConfig]) -> list[Strategy]:
    out: list[Strategy] = []
    for cfg in configs:
        if isinstance(cfg, DirectionalConfig):
            out.append(DirectionalStrategy(cfg))
        elif isinstance(cfg, IronCondorConfig):
            out.append(IronCondorStrategy(cfg))
        elif isinstance(cfg, VolStrangleConfig):
            out.append(VolStrangleStrategy(cfg))
    return out


def _risk_decision_to_reason(rd: RiskDecision) -> DecisionReason:
    mapping: dict[RiskDecision, DecisionReason] = {
        RiskDecision.APPROVED: DecisionReason.PASSED,
        RiskDecision.OUTSIDE_TRADING_WINDOW: DecisionReason.OUTSIDE_TRADING_WINDOW,
        RiskDecision.CIRCUIT_BREAKER: DecisionReason.CIRCUIT_BREAKER,
        RiskDecision.DAILY_CAP_TRIPPED: DecisionReason.DAILY_CAP_TRIPPED,
        RiskDecision.WEEKLY_CAP_TRIPPED: DecisionReason.WEEKLY_CAP_TRIPPED,
        RiskDecision.STRATEGY_MAX_CONCURRENT: DecisionReason.STRATEGY_MAX_CONCURRENT,
        RiskDecision.GLOBAL_MAX_CONCURRENT: DecisionReason.GLOBAL_MAX_CONCURRENT,
        RiskDecision.ZERO_LOTS_AFTER_FLOOR: DecisionReason.ZERO_LOTS_AFTER_FLOOR,
        RiskDecision.PREMIUM_ABOVE_RISK_BUDGET: DecisionReason.PREMIUM_ABOVE_RISK_BUDGET,
        RiskDecision.CONDOR_MAX_LOSS_ABOVE_BUDGET: DecisionReason.CONDOR_MAX_LOSS_ABOVE_BUDGET,
        RiskDecision.STRANGLE_PREMIUM_ABOVE_RISK_BUDGET: DecisionReason.STRANGLE_PREMIUM_ABOVE_RISK_BUDGET,
        RiskDecision.STRATEGY_DISABLED: DecisionReason.STRATEGY_DISABLED,
    }
    return mapping.get(rd, DecisionReason.OTHER)


def _risk_record(intent: Intent, sizing: SizingResult) -> dict[str, Any]:
    reason = _risk_decision_to_reason(sizing.decision)
    sym = intent.legs[0].symbol if intent.legs else None
    fv: dict[str, Any] = {"risk_decision": sizing.decision.value}
    for k, v in (sizing.notes or {}).items():
        fv[k] = v
    return {
        "strategy_id": intent.strategy_id.value,
        "kind": DecisionKind.RISK.value,
        "underlying": intent.underlying.value,
        "symbol": sym,
        "passed": sizing.approved,
        "reason": reason.value,
        "feature_vector": fv,
    }


def _quote_from_ws_ticker(msg: dict[str, Any]) -> QuoteSnapshot | None:
    sym = msg.get("symbol")
    if not isinstance(sym, str):
        return None
    if parse_symbol(sym) is None:
        return None

    def _f(v: Any) -> float | None:
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    raw_g = msg.get("greeks")
    g: dict[str, Any] = raw_g if isinstance(raw_g, dict) else {}
    raw_q = msg.get("quotes")
    q: dict[str, Any] = raw_q if isinstance(raw_q, dict) else {}
    return QuoteSnapshot(
        symbol=sym,
        bid=_f(q.get("best_bid") or msg.get("best_bid")),
        ask=_f(q.get("best_ask") or msg.get("best_ask")),
        mark_price=_f(msg.get("mark_price")),
        iv=_f(g.get("iv") or msg.get("iv")),
        delta=_f(g.get("delta")),
        gamma=_f(g.get("gamma")),
        theta=_f(g.get("theta")),
        vega=_f(g.get("vega")),
        rho=_f(g.get("rho")),
        underlying_mark=_f(msg.get("spot_price") or msg.get("underlying_mark")),
    )


def _mark_underlying(symbol: str) -> Underlying | None:
    if symbol == "MARK:BTCUSD":
        return Underlying.BTC
    if symbol == "MARK:ETHUSD":
        return Underlying.ETH
    return None


async def _load_peak_nav(db: Database, fallback: float) -> float:
    async with db.session() as session:
        row = (
            await session.execute(select(NavHistory).order_by(NavHistory.trading_date.desc()).limit(1))
        ).scalar_one_or_none()
    if row is None:
        return fallback
    return max(float(fallback), float(row.peak_nav_inr))


async def _load_open_trades(db: Database) -> list[tuple[Trade, list[Leg]]]:
    async with db.session() as session:
        stmt = (
            select(Trade)
            .where(Trade.status == TradeStatus.OPEN.value)
            .options(selectinload(Trade.legs))
            .order_by(Trade.id)
        )
        trades = list((await session.execute(stmt)).scalars().all())
        return [(t, list(t.legs)) for t in trades]


def _expiry_from_leg_symbol(symbol: str) -> dt.datetime:
    parsed = parse_symbol(symbol)
    if parsed is not None:
        return parsed[3]
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _trade_to_position_state(trade: Trade, legs: list[Leg], chain: ChainCache) -> PositionState:
    leg_states: list[dict[str, Any]] = []
    for leg in legs:
        q = chain.get_quote(leg.symbol)
        leg_states.append(
            {
                "symbol": leg.symbol,
                "option_type": leg.option_type,
                "current_mid": q.mid if q is not None else None,
            }
        )
    notes = dict(trade.notes or {})
    expiry: dt.datetime | None = trade.expiry
    if expiry is None and legs:
        expiry = _expiry_from_leg_symbol(legs[0].symbol)
    if expiry is None:
        expiry = dt.datetime.now(dt.UTC).replace(tzinfo=None)

    return PositionState(
        trade_id=trade.id,
        strategy_id=StrategyId(trade.strategy_id),
        underlying=Underlying(trade.underlying),
        expiry=expiry,
        lots=trade.lots,
        entry_ts=trade.entry_ts,
        entry_premium_inr=trade.premium_paid_inr,
        entry_credit_inr=trade.credit_received_inr,
        entry_underlying_price=float(notes["entry_underlying_price"])
        if notes.get("entry_underlying_price") is not None
        else None,
        entry_atr=float(notes["entry_atr"]) if notes.get("entry_atr") is not None else None,
        current_stop_price=_note_float(notes, "current_stop_price"),
        current_trail_stop_price=_note_float(notes, "current_trail_stop_price"),
        peak_pnl_inr=_note_float(notes, "peak_pnl_inr"),
        leg_states=leg_states,
        notes=notes,
    )


async def _accounting_snapshot(db: Database) -> TradeAccountingSnapshot:
    open_rows = await _load_open_trades(db)
    by_s: dict[StrategyId, int] = defaultdict(int)
    for trade, _legs in open_rows:
        by_s[StrategyId(trade.strategy_id)] += 1
    return TradeAccountingSnapshot(
        open_count_total=len(open_rows),
        open_count_by_strategy=dict(by_s),
    )


async def _persist_entry_and_execute(
    *,
    db: Database,
    executor: DryExecutor,
    intent: Intent,
    sizing: SizingResult,
    market: MarketState,
    mode: str,
    journal: TradeJournal,
    wallet_at_entry: dict[str, Any] | None,
) -> None:
    now = market.now
    spot = market.spot(intent.underlying)
    atr_guess = None
    if intent.feature_vector and "atr" in intent.feature_vector:
        try:
            atr_guess = float(intent.feature_vector["atr"])
        except (TypeError, ValueError):
            atr_guess = None

    first = intent.legs[0]
    trade_expiry = max((leg.expiry for leg in intent.legs), default=first.expiry)
    async with db.session() as session:
        sig = Signal(
            ts=now,
            strategy_id=intent.strategy_id.value,
            underlying=intent.underlying.value,
            side=None,
            intended_symbol=first.symbol,
            intended_expiry=first.expiry,
            intended_strike=first.strike,
            intended_lots=sizing.sized_lots,
            intended_premium_inr=intent.target_premium_inr or intent.target_credit_inr or None,
            feature_vector=dict(intent.feature_vector or {}),
        )
        session.add(sig)
        await session.flush()
        ind_entry = indicator_snapshot_for_underlying(intent.underlying, market)
        trade = Trade(
            strategy_id=intent.strategy_id.value,
            underlying=intent.underlying.value,
            expiry=trade_expiry,
            entry_ts=now,
            status=TradeStatus.OPEN.value,
            mode=mode,
            lots=sizing.sized_lots,
            signal_id=sig.id,
            notes={
                "entry_underlying_price": spot,
                "entry_atr": atr_guess,
                "rationale": intent.rationale,
                "wallet_at_entry": wallet_at_entry,
                "indicators_at_entry": ind_entry,
            },
        )
        session.add(trade)
        await session.flush()
        trade_id = trade.id

    req = EntryRequest(
        strategy_id=intent.strategy_id,
        trade_id=trade_id,
        underlying=intent.underlying,
        legs=[
            LegIntent(
                symbol=leg.symbol,
                side=leg.side,
                option_type=leg.option_type,
                strike=leg.strike,
                expiry=leg.expiry,
            )
            for leg in intent.legs
        ],
        lots=sizing.sized_lots,
        intent_rationale=intent.rationale,
        spread_pct_max=intent.spread_pct_max,
    )
    result = await executor.submit_entry(req)
    if not result.success:
        async with db.session() as session:
            t = await session.get(Trade, trade_id)
            if t is not None:
                t.status = TradeStatus.ERRORED.value
                t.notes = {**(t.notes or {}), "error": result.error or "entry_failed"}
        logger.error("dry entry failed trade_id={} err={}", trade_id, result.error)
        return

    net = result.total_premium_inr
    prem = net if net > 0 else None
    cred = -net if net < 0 else None
    slip_vals = [f.slippage_bps for f in result.fills if f.slippage_bps is not None]
    slip = float(sum(slip_vals) / len(slip_vals)) if slip_vals else None

    async with db.session() as session:
        t = await session.get(Trade, trade_id)
        if t is None:
            return
        t.premium_paid_inr = prem
        t.credit_received_inr = cred
        t.slippage_bps = slip
        notes = dict(t.notes or {})
        notes["entry_fill_prices"] = {str(f.leg_idx): f.avg_fill_price for f in result.fills}
        notes["entry_net_premium_inr"] = net
        notes["trade_lifecycle"] = "opened_filled"
        t.notes = notes
        for fill in result.fills:
            session.add(
                Order(
                    ts=result.completed_at or now,
                    strategy_id=intent.strategy_id.value,
                    trade_id=trade_id,
                    leg_idx=fill.leg_idx,
                    client_order_id=fill.client_order_id,
                    symbol=fill.symbol,
                    side=fill.side.value,
                    order_type=OrderType.MARKET.value,
                    limit_price=None,
                    qty=fill.qty_requested,
                    filled_qty=fill.qty_filled,
                    filled_price=fill.avg_fill_price,
                    state=OrderState.FILLED.value,
                    raw_response=fill.raw_response,
                )
            )
            li = intent.legs[fill.leg_idx] if fill.leg_idx < len(intent.legs) else intent.legs[-1]
            session.add(
                Leg(
                    trade_id=trade_id,
                    strategy_id=intent.strategy_id.value,
                    leg_idx=fill.leg_idx,
                    symbol=fill.symbol,
                    option_type=li.option_type,
                    strike=li.strike,
                    side=li.side,
                    lots=sizing.sized_lots,
                    entry_price=fill.avg_fill_price,
                    status=LegStatus.OPEN.value,
                )
            )
    logger.info(
        "TRADE_OPEN trade_id={} strategy={} lots={} net_premium_inr={:.4f} wallet={} spot={}",
        trade_id,
        intent.strategy_id.value,
        sizing.sized_lots,
        net,
        wallet_at_entry.get("balances") if isinstance(wallet_at_entry, dict) else wallet_at_entry,
        spot,
    )
    try:
        await journal.write_open_trade(trade_id)
    except OSError as exc:
        logger.warning("open trade journal write failed: {}", exc)


async def _persist_trail_stop(
    *,
    db: Database,
    executor: DryExecutor,
    trade: Trade,
    legs: list[Leg],
    trail: TrailAction,
    journal: TradeJournal,
    wallet_snapshot: dict[str, Any] | None,
) -> None:
    if not legs:
        return
    leg = legs[0]
    side = LegSide.BUY if leg.side == "buy" else LegSide.SELL
    client_id = f"trail-{trade.id}-{leg.leg_idx}-{int(time.time())}"
    await executor.update_stop(
        trade.id,
        leg.symbol,
        side,
        float(trade.lots),
        trail.new_stop_price,
        client_id,
    )
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    async with db.session() as session:
        t = await session.get(Trade, trade.id)
        if t is None:
            return
        notes = dict(t.notes or {})
        notes["current_trail_stop_price"] = trail.new_stop_price
        ev = list(notes.get("trail_events") or [])
        ev.append(
            {
                "ts": now.isoformat(),
                "new_stop": trail.new_stop_price,
                "notes": dict(trail.notes or {}),
                "wallet": wallet_snapshot,
            }
        )
        notes["trail_events"] = ev
        t.notes = notes
    logger.info(
        "TRAIL trade_id={} new_stop={} wallet_balances={}",
        trade.id,
        trail.new_stop_price,
        wallet_snapshot.get("balances") if isinstance(wallet_snapshot, dict) else None,
    )
    try:
        await journal.write_open_trade(trade.id)
    except OSError as exc:
        logger.warning("open trade journal after trail: {}", exc)


async def _persist_exit(
    *,
    db: Database,
    executor: DryExecutor,
    trade: Trade,
    legs: list[Leg],
    trigger: ExitTrigger,
    journal: TradeJournal,
    metrics: MetricsRegistry,
    nav: NavTracker,
    wallet_at_exit: dict[str, Any] | None,
    indicator_at_exit: dict[str, Any] | None,
) -> None:
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    leg_intents = [
        LegIntent(
            symbol=leg.symbol,
            side=leg.side,
            option_type=leg.option_type or "call",
            strike=leg.strike or 0.0,
            expiry=trade.expiry or _expiry_from_leg_symbol(leg.symbol),
        )
        for leg in sorted(legs, key=lambda x: x.leg_idx)
    ]
    req = ExitRequest(
        strategy_id=StrategyId(trade.strategy_id),
        trade_id=trade.id,
        underlying=Underlying(trade.underlying),
        legs=leg_intents,
        lots=trade.lots,
        trigger=trigger,
    )
    res = await executor.submit_exit(req)
    if not res.success:
        logger.error("dry exit failed trade_id={} err={}", trade.id, res.error)
        return

    entry_cash = 0.0
    if trade.premium_paid_inr:
        entry_cash -= float(trade.premium_paid_inr) * trade.lots
    if trade.credit_received_inr:
        entry_cash += float(trade.credit_received_inr) * trade.lots

    exit_cash = 0.0
    for f in res.fills:
        if f.avg_fill_price is None:
            continue
        sign = 1.0 if f.side == LegSide.SELL else -1.0
        exit_cash += sign * f.avg_fill_price * f.qty_filled

    realised = exit_cash + entry_cash
    nav.nav_now = float(nav.nav_now) + realised
    outcome = {
        "exit_trigger": trigger.value,
        "realised_pnl_inr": realised,
        "result": "win" if realised > 1e-6 else ("loss" if realised < -1e-6 else "flat"),
    }

    async with db.session() as session:
        t = await session.get(Trade, trade.id)
        if t is None:
            return
        notes = dict(t.notes or {})
        notes["wallet_at_exit"] = wallet_at_exit
        notes["indicators_at_exit"] = indicator_at_exit
        notes["trade_outcome"] = outcome
        peak_note = _note_float(notes, "peak_pnl_inr")
        if peak_note is not None:
            t.peak_pnl_inr = peak_note
        t.notes = notes
        t.status = TradeStatus.CLOSED.value
        t.exit_ts = res.completed_at or now
        t.realised_pnl_inr = realised
        t.exit_reason = trigger.value
        for leg in legs:
            db_leg = await session.get(Leg, leg.id)
            if db_leg is None:
                continue
            fill = next((f for f in res.fills if f.symbol == db_leg.symbol), None)
            db_leg.status = LegStatus.CLOSED.value
            db_leg.exit_price = fill.avg_fill_price if fill else None
            db_leg.pnl_inr = None
        session.add(
            Order(
                ts=res.completed_at or now,
                strategy_id=trade.strategy_id,
                trade_id=trade.id,
                leg_idx=None,
                client_order_id=f"exit-{trade.id}-{int(time.time())}",
                symbol=legs[0].symbol if legs else "",
                side="sell",
                order_type=OrderType.MARKET.value,
                qty=float(trade.lots),
                filled_qty=float(trade.lots),
                state=OrderState.FILLED.value,
            )
        )

    metrics.trades_closed_total.labels(trade.strategy_id, trigger.value).inc()
    metrics.trade_pnl_inr.labels(trade.strategy_id).observe(realised)
    path = await journal.write_for_trade(trade.id)
    logger.info(
        "TRADE_CLOSE trade_id={} pnl={:.2f} outcome={} wallet_exit={} journal={}",
        trade.id,
        realised,
        outcome["result"],
        wallet_at_exit.get("balances") if isinstance(wallet_at_exit, dict) else wallet_at_exit,
        path,
    )


async def run_trading_engine() -> None:
    app_config = load_all()
    settings = app_config.settings
    configure_logging(settings)
    logger.info(
        "crypto-options-bot engine starting mode={} config_dir={}",
        settings.mode.value,
        settings.config_dir,
    )

    db = get_database(settings.db_url)
    nav_now = float(app_config.effective_nav_inr)
    peak = await _load_peak_nav(db, nav_now)
    nav_tracker = NavTracker(
        nav_now=nav_now,
        nav_open_today=nav_now,
        nav_open_week=nav_now,
        peak_nav=max(peak, nav_now),
        circuit_breaker_tripped=False,
    )

    strategy_cfgs = {s.id: s for s in app_config.strategies}
    risk = RiskManager(
        global_config=app_config.global_config,
        nav_tracker=nav_tracker,
        strategy_configs=strategy_cfgs,
    )
    registry = StrategyRegistry(_build_strategies(app_config.strategies))
    dispatcher = StrategyDispatcher(registry)

    rest = DeltaRestClient(settings)
    chain = ChainCache(rest)
    executor = DryExecutor(chain)
    decision_writer = DecisionLogWriter(
        db,
        mirror_path=settings.logs_dir / "decisions.jsonl",
    )
    journal = TradeJournal(db, journals_dir=settings.journals_dir)
    daily_agg = DailyAggregator(db, reports_dir=settings.reports_dir)
    metrics = MetricsRegistry()
    textfile = TextfileCollector(metrics, settings.prom_textfile_path)

    subscriptions = [
        Subscription("v2/ticker", ("MARK:BTCUSD", "MARK:ETHUSD")),
    ]
    ws = DeltaWebSocketClient(settings, subscriptions)

    candles: dict[Underlying, dict[str, CandleAggregator]] = {}
    for u in (Underlying.BTC, Underlying.ETH):
        candles[u] = {
            "15m": CandleAggregator("15m", history=512),
            "1h": CandleAggregator("1h", history=512),
        }

    underlying_marks: dict[Underlying, float] = {}
    stop = asyncio.Event()

    async def liveness() -> bool:
        # REST chain refresh can succeed before first WS mark; strategies tolerate missing spot briefly.
        return chain.size > 0

    async def liveness_extra() -> dict[str, object]:
        return {
            "mode": settings.mode.value,
            "skeleton": False,
            "chain_instruments": chain.size,
            "marks": {k.value: v for k, v in underlying_marks.items()},
            "ws_connected": ws.stats.connected,
        }

    server = MetricsServer(
        metrics,
        host="0.0.0.0",
        port=settings.prom_http_port,
        liveness_check=liveness,
        liveness_extra=liveness_extra,
    )
    await server.start()

    last_textfile = time.monotonic()
    last_daily_ist_date: dt.date | None = None
    tick_interval = 5.0
    open_journal_last: dict[int, float] = {}
    engine_tick: dict[str, Any] = {"wallet_mono": 0.0, "open_journal": open_journal_last}

    async def chain_refresh_loop() -> None:
        while not stop.is_set():
            try:
                await chain.refresh_instruments()
                await chain.refresh_quotes()
            except DeltaRestError as exc:
                logger.warning("chain refresh: {}", exc)
                metrics.rest_errors_total.labels(exc.endpoint or "unknown", str(exc.status_code or 0)).inc()
            except Exception:
                logger.exception("chain refresh failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=15.0)
            if stop.is_set():
                break

    async def ws_runner() -> None:
        try:
            await ws.run()
        except asyncio.CancelledError:
            raise

    async def ws_drain_loop() -> None:
        while not stop.is_set():
            try:
                msg = await asyncio.wait_for(ws.queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            if msg.get("type") != "v2/ticker":
                continue
            sym = msg.get("symbol")
            if not isinstance(sym, str):
                continue
            u = _mark_underlying(sym)
            if u is not None:
                mp = msg.get("mark_price")
                if mp is None:
                    mp = msg.get("spot_price") or msg.get("underlying_mark")
                if mp is not None:
                    try:
                        px = float(mp)
                        underlying_marks[u] = px
                        now_ts = dt.datetime.now(dt.UTC).replace(tzinfo=None)
                        for agg in candles[u].values():
                            agg.add_tick(now_ts, px, 0.0)
                    except (TypeError, ValueError):
                        pass
                continue
            snap = _quote_from_ws_ticker(msg)
            if snap is not None:
                chain.upsert_quote(snap)

    async def daily_loop() -> None:
        nonlocal last_daily_ist_date
        ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=60.0)
            if stop.is_set():
                break
            now_ist = dt.datetime.now(dt.UTC).astimezone(ist)
            if now_ist.hour != 22 or now_ist.minute < 35:
                continue
            today_ist = now_ist.date()
            if last_daily_ist_date == today_ist:
                continue
            try:
                await daily_agg.run(
                    trading_date=today_ist,
                    nav_inr=float(nav_tracker.nav_now),
                    peak_nav_inr=float(max(nav_tracker.peak_nav, nav_tracker.nav_now)),
                    circuit_breaker_tripped=nav_tracker.circuit_breaker_tripped,
                )
                last_daily_ist_date = today_ist
                logger.info("daily aggregator completed for {}", today_ist)
            except Exception:
                logger.exception("daily aggregator failed")

    async def main_loop() -> None:
        nonlocal last_textfile
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=tick_interval)
                continue
            except TimeoutError:
                pass

            now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
            candles_by_tf: dict[Underlying, dict[str, list[Any]]] = {}
            for u, aggs in candles.items():
                candles_by_tf[u] = {
                    "15m": list(aggs["15m"].closed) + ([aggs["15m"].current] if aggs["15m"].current else []),
                    "1h": list(aggs["1h"].closed) + ([aggs["1h"].current] if aggs["1h"].current else []),
                }

            market = MarketState(
                now=now,
                chain=chain,
                candles_by_tf=candles_by_tf,
                underlying_marks=dict(underlying_marks),
            )

            wallet_snap: dict[str, Any] | None = None
            api_ok = bool(settings.delta_api_key and settings.delta_api_secret)
            mono = time.monotonic()
            if api_ok and mono - float(engine_tick["wallet_mono"]) >= WALLET_POLL_INTERVAL_S:
                try:
                    wallet_snap = await fetch_wallet_snapshot(rest)
                    engine_tick["wallet_mono"] = mono
                except DeltaRestError as exc:
                    logger.debug("wallet poll skipped: {}", exc)

            open_rows = await _load_open_trades(db)
            if open_rows:
                await refresh_all_open_trades(db, open_rows, chain, market, wallet_snapshot=wallet_snap)
            open_rows = await _load_open_trades(db)
            positions = [_trade_to_position_state(t, legs, chain) for t, legs in open_rows]
            disp = dispatcher.evaluate_all(market)
            disp = dispatcher.manage_all(positions, market, disp)

            records: list[dict[str, Any]] = list(disp.all_decisions)

            for tid, actions in disp.actions_by_position.items():
                trade = next((t for t, _ in open_rows if t.id == tid), None)
                legs = next((ls for t, ls in open_rows if t.id == tid), [])
                if trade is None:
                    continue
                for act in actions:
                    if act.kind == ActionType.CLOSE and act.close is not None:
                        wallet_exit: dict[str, Any] | None = None
                        if api_ok:
                            try:
                                wallet_exit = await fetch_wallet_snapshot(rest)
                            except DeltaRestError as exc:
                                logger.debug("wallet at exit: {}", exc)
                        ind_exit = indicator_snapshot_for_trade(trade, market)
                        if settings.is_dry:
                            await _persist_exit(
                                db=db,
                                executor=executor,
                                trade=trade,
                                legs=legs,
                                trigger=act.close.reason,
                                journal=journal,
                                metrics=metrics,
                                nav=nav_tracker,
                                wallet_at_exit=wallet_exit,
                                indicator_at_exit=ind_exit,
                            )
                        else:
                            logger.warning("live exit not executed (dry-only engine) trade_id={}", tid)
                    elif act.kind == ActionType.TRAIL_STOP and act.trail is not None and legs:
                        if settings.is_dry:
                            await _persist_trail_stop(
                                db=db,
                                executor=executor,
                                trade=trade,
                                legs=legs,
                                trail=act.trail,
                                journal=journal,
                                wallet_snapshot=wallet_snap,
                            )
                        else:
                            logger.warning("live trail not executed trade_id={}", tid)

            accounting = await _accounting_snapshot(db)
            for intent in disp.all_intents:
                sizing = risk.gate(intent, now_utc=now, accounting=accounting)
                records.append(_risk_record(intent, sizing))
                metrics.intents_total.labels(intent.strategy_id.value).inc()
                if sizing.approved and settings.is_dry:
                    entry_wallet = wallet_snap
                    if entry_wallet is None and api_ok:
                        try:
                            entry_wallet = await fetch_wallet_snapshot(rest)
                        except DeltaRestError:
                            entry_wallet = None
                    await _persist_entry_and_execute(
                        db=db,
                        executor=executor,
                        intent=intent,
                        sizing=sizing,
                        market=market,
                        mode="dry",
                        journal=journal,
                        wallet_at_entry=entry_wallet,
                    )
                    metrics.trades_opened_total.labels(intent.strategy_id.value).inc()
                    accounting = await _accounting_snapshot(db)
                elif sizing.approved and not settings.is_dry:
                    logger.warning("intent approved but live execution is not enabled in engine")

            open_after = await _load_open_trades(db)
            m_journal = time.monotonic()
            jlast: dict[int, float] = engine_tick["open_journal"]
            for t, _legs in open_after:
                if m_journal - float(jlast.get(t.id, 0.0)) >= OPEN_JOURNAL_REFRESH_S:
                    try:
                        await journal.write_open_trade(t.id)
                        jlast[t.id] = m_journal
                    except OSError as exc:
                        logger.debug("open trade journal refresh: {}", exc)

            n = await decision_writer.write(records)
            for row in records:
                sid = str(row.get("strategy_id", "unknown"))
                passed = "true" if row.get("passed") else "false"
                reason = str(row.get("reason", "unknown"))
                metrics.decisions_total.labels(sid, passed, reason).inc()
            metrics.ticks_total.inc()
            metrics.last_tick_seconds.set(time.time())
            nav_tracker.peak_nav = max(float(nav_tracker.peak_nav), float(nav_tracker.nav_now))
            metrics.nav_inr.set(float(nav_tracker.nav_now))
            metrics.peak_nav_inr.set(float(nav_tracker.peak_nav))
            for sid in registry.all_ids:
                c = accounting.open_count_by_strategy.get(sid, 0)
                metrics.open_positions.labels(sid.value).set(c)

            if time.monotonic() - last_textfile > 60.0:
                try:
                    textfile.write_once()
                except OSError as exc:
                    logger.debug("textfile metrics: {}", exc)
                last_textfile = time.monotonic()

            logger.debug("tick decisions_written={} intents={}", n, len(disp.all_intents))

    refresh_task = asyncio.create_task(chain_refresh_loop(), name="chain-refresh")
    ws_task = asyncio.create_task(ws_runner(), name="ws-run")
    drain_task = asyncio.create_task(ws_drain_loop(), name="ws-drain")
    daily_task = asyncio.create_task(daily_loop(), name="daily")
    main_task = asyncio.create_task(main_loop(), name="main")

    loop = asyncio.get_running_loop()

    def _stop() -> None:
        logger.info("shutdown requested")
        stop.set()
        ws.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _stop)

    try:
        await stop.wait()
    except asyncio.CancelledError:
        raise
    finally:
        for t in (main_task, daily_task, drain_task, refresh_task, ws_task):
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        ws.stop()
        await rest.aclose()
        await server.stop()
        await db.aclose()
        logger.info("trading engine stopped")


__all__ = ["run_trading_engine"]
