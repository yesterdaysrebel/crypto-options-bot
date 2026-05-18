"""Live trading engine: Delta REST/WS, chain cache, strategy dispatch, dry execution, analytics."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import signal
import time
from collections import defaultdict
from collections.abc import Mapping
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
from bot.desk.greek_snapshot import greeks_by_symbol, trade_iv_from_symbols
from bot.desk.iv_history import IvHistoryStore
from bot.desk.portfolio_greeks import PortfolioGreeks
from bot.exchange.rest import DeltaRestClient, DeltaRestError
from bot.exchange.wallet import fetch_wallet_snapshot
from bot.exchange.ws import DeltaWebSocketClient, Subscription
from bot.execution.dry import DryExecutor
from bot.execution.live import LiveExecutor
from bot.execution.premium import per_lot_premium_from_net, realised_pnl_inr
from bot.execution.router import EntryRequest, ExecutionRouter, ExitRequest, LegSide
from bot.exits import ExitEngine, ExitKind, PositionRuntime
from bot.observability.logging_setup import configure_logging
from bot.observability.metrics import MetricsRegistry, TextfileCollector
from bot.observability.server import MetricsServer
from bot.risk.caps import NavTracker
from bot.risk.manager import RiskDecision, RiskManager, SizingResult, TradeAccountingSnapshot
from bot.runtime.iv_prefetch import prefetch_iv_for_strategies
from bot.runtime.nav_state import (
    load_nav_tracker,
    maybe_roll_ist_trading_day,
    sync_circuit_breaker_from_risk,
)
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
from bot.strategies.base import ExitTrigger, Intent, LegIntent, TrailAction

WALLET_POLL_INTERVAL_S = 30.0
OPEN_JOURNAL_REFRESH_S = 30.0


def _apply_directional_exit_cooldown(
    registry: StrategyRegistry,
    trade: Trade,
    *,
    now: dt.datetime,
) -> None:
    try:
        strat = registry.get(StrategyId(trade.strategy_id))
    except KeyError:
        return
    if not isinstance(strat, DirectionalStrategy):
        return
    minutes = float(strat.config.entry.cooldown_minutes_after_underlying_stop)
    if minutes <= 0:
        return
    until = now + dt.timedelta(minutes=minutes)
    strat.context.set_underlying_cooldown(trade.underlying, until)


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
        RiskDecision.LOW_OPEN_INTEREST: DecisionReason.LOW_OPEN_INTEREST,
        RiskDecision.MISSING_GREEKS: DecisionReason.MISSING_GREEKS,
        RiskDecision.PORTFOLIO_DELTA_LIMIT: DecisionReason.PORTFOLIO_DELTA_LIMIT,
        RiskDecision.PORTFOLIO_VEGA_LIMIT: DecisionReason.PORTFOLIO_VEGA_LIMIT,
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
        open_interest=_f(msg.get("oi_contracts") or msg.get("open_interest") or msg.get("oi")),
        volume_24h=_f(msg.get("volume") or msg.get("volume_24h")),
    )


def _mark_underlying(symbol: str) -> Underlying | None:
    """Map Delta ``v2/ticker`` contract symbol to BTC/ETH for index marks.

    Delta India docs subscribe with ``BTCUSD`` / ``ETHUSD``; ``MARK:*`` may appear on
    some feeds. Both shapes are accepted here.
    """
    if symbol in ("MARK:BTCUSD", "BTCUSD"):
        return Underlying.BTC
    if symbol in ("MARK:ETHUSD", "ETHUSD"):
        return Underlying.ETH
    return None


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
    executor: ExecutionRouter,
    intent: Intent,
    sizing: SizingResult,
    market: MarketState,
    mode: str,
    journal: TradeJournal,
    wallet_at_entry: dict[str, Any] | None,
    quote_for: Mapping[str, QuoteSnapshot] | None = None,
    chain: ChainCache | None = None,
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
    prem, cred = per_lot_premium_from_net(net, sizing.sized_lots)
    slip_vals = [f.slippage_bps for f in result.fills if f.slippage_bps is not None]
    slip = float(sum(slip_vals) / len(slip_vals)) if slip_vals else None

    symbols = [leg.symbol for leg in intent.legs]
    entry_iv: float | None = None
    entry_greeks: dict[str, dict[str, float | None]] = {}
    if quote_for is not None:
        entry_iv = trade_iv_from_symbols(symbols, quote_for, chain=chain)
        entry_greeks = greeks_by_symbol(symbols, quote_for, chain=chain)

    async with db.session() as session:
        t = await session.get(Trade, trade_id)
        if t is None:
            return
        t.premium_paid_inr = prem
        t.credit_received_inr = cred
        t.slippage_bps = slip
        if entry_iv is not None:
            t.entry_iv = entry_iv
        notes = dict(t.notes or {})
        notes["entry_fill_prices"] = {str(f.leg_idx): f.avg_fill_price for f in result.fills}
        notes["entry_net_premium_inr"] = net
        notes["trade_lifecycle"] = "opened_filled"
        if entry_greeks:
            notes["entry_greeks"] = entry_greeks
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
    executor: ExecutionRouter,
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
    executor: ExecutionRouter,
    trade: Trade,
    legs: list[Leg],
    trigger: ExitTrigger,
    journal: TradeJournal,
    metrics: MetricsRegistry,
    nav: NavTracker,
    wallet_at_exit: dict[str, Any] | None,
    indicator_at_exit: dict[str, Any] | None,
    quote_for: Mapping[str, QuoteSnapshot] | None = None,
    chain: ChainCache | None = None,
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

    realised = realised_pnl_inr(
        premium_paid_per_lot=trade.premium_paid_inr,
        credit_received_per_lot=trade.credit_received_inr,
        lots=trade.lots,
        exit_fills=res.fills,
    )
    nav.nav_now = float(nav.nav_now) + realised
    outcome = {
        "exit_trigger": trigger.value,
        "realised_pnl_inr": realised,
        "result": "win" if realised > 1e-6 else ("loss" if realised < -1e-6 else "flat"),
    }

    symbols = [leg.symbol for leg in legs]
    exit_iv: float | None = None
    exit_greeks: dict[str, dict[str, float | None]] = {}
    if quote_for is not None:
        exit_iv = trade_iv_from_symbols(symbols, quote_for, chain=chain)
        exit_greeks = greeks_by_symbol(symbols, quote_for, chain=chain)

    async with db.session() as session:
        t = await session.get(Trade, trade.id)
        if t is None:
            return
        notes = dict(t.notes or {})
        notes["wallet_at_exit"] = wallet_at_exit
        notes["indicators_at_exit"] = indicator_at_exit
        notes["trade_outcome"] = outcome
        if exit_greeks:
            notes["exit_greeks"] = exit_greeks
        if exit_iv is not None:
            t.exit_iv = exit_iv
        entry_spot = _note_float(notes, "entry_underlying_price")
        exit_spot: float | None = None
        if indicator_at_exit and isinstance(indicator_at_exit, dict):
            spot_raw = indicator_at_exit.get("spot")
            if spot_raw is not None:
                try:
                    exit_spot = float(spot_raw)
                except (TypeError, ValueError):
                    exit_spot = None
        from bot.desk.pnl_attribution import estimate_delta_pnl_inr

        usd_inr_rate = _note_float(notes, "usd_inr_rate") or 85.0
        delta_pnl = estimate_delta_pnl_inr(
            t,
            legs,
            entry_underlying_price=entry_spot,
            exit_underlying_price=exit_spot,
            usd_inr_rate=usd_inr_rate,
            chain=chain,
        )
        if delta_pnl is not None:
            t.delta_pnl_inr = delta_pnl
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
        exit_ts = res.completed_at or now
        for fill in res.fills:
            if fill.avg_fill_price is None:
                continue
            session.add(
                Order(
                    ts=exit_ts,
                    strategy_id=trade.strategy_id,
                    trade_id=trade.id,
                    leg_idx=fill.leg_idx,
                    client_order_id=fill.client_order_id,
                    symbol=fill.symbol,
                    side=fill.side.value,
                    order_type=OrderType.MARKET.value,
                    qty=fill.qty_requested,
                    filled_qty=fill.qty_filled,
                    filled_price=fill.avg_fill_price,
                    state=OrderState.FILLED.value,
                    raw_response=fill.raw_response,
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

    if settings.is_live and not (settings.delta_api_key and settings.delta_api_secret):
        raise RuntimeError("MODE=live requires DELTA_API_KEY and DELTA_API_SECRET")

    db = get_database(settings.db_url)
    nav_tracker = await load_nav_tracker(db, base_nav_inr=float(app_config.effective_nav_inr))
    breaker_marker = settings.runtime_dir / "circuit_breaker.json"
    if breaker_marker.exists():
        nav_tracker.circuit_breaker_tripped = True

    strategy_cfgs = {s.id: s for s in app_config.strategies}
    risk = RiskManager(
        global_config=app_config.global_config,
        nav_tracker=nav_tracker,
        strategy_configs=strategy_cfgs,
    )
    registry = StrategyRegistry(_build_strategies(app_config.strategies))
    dispatcher = StrategyDispatcher(registry)
    exit_engine = ExitEngine(
        registry,
        trail_update_throttle_seconds=float(app_config.global_config.execution.trail_update_throttle_seconds),
    )

    rest = DeltaRestClient(settings)
    chain = ChainCache(rest)
    iv_history = IvHistoryStore(db)
    exec_cfg = app_config.global_config.execution
    if settings.is_live:
        executor: ExecutionRouter = LiveExecutor(
            rest,
            chain,
            slip_bps_directional=exec_cfg.slip_bps_directional,
            slip_bps_condor=exec_cfg.slip_bps_condor,
            slip_bps_strangle=exec_cfg.slip_bps_strangle,
        )
        logger.warning("LIVE mode: real orders will be sent to Delta Exchange India")
    else:
        executor = DryExecutor(
            chain,
            slip_bps_directional=exec_cfg.slip_bps_directional,
            slip_bps_condor=exec_cfg.slip_bps_condor,
            slip_bps_strangle=exec_cfg.slip_bps_strangle,
        )
    decision_writer = DecisionLogWriter(
        db,
        mirror_path=settings.logs_dir / "decisions.jsonl",
    )
    journal = TradeJournal(db, journals_dir=settings.journals_dir)
    daily_agg = DailyAggregator(db, reports_dir=settings.reports_dir)
    metrics = MetricsRegistry()
    textfile = TextfileCollector(metrics, settings.prom_textfile_path)

    subscriptions = [
        # Delta India ``v2/ticker`` expects perpetual symbols (see API websocket guide).
        Subscription("v2/ticker", ("BTCUSD", "ETHUSD")),
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
        host=settings.prom_http_host,
        port=settings.prom_http_port,
        liveness_check=liveness,
        liveness_extra=liveness_extra,
    )
    await server.start()

    last_textfile = time.monotonic()
    last_daily_ist_date: dt.date | None = None
    last_ist_trading_date: dt.date | None = None
    tick_interval = 5.0
    usd_inr = float(app_config.effective_usd_inr_rate)
    open_journal_last: dict[int, float] = {}
    position_runtimes: dict[int, PositionRuntime] = {}
    engine_tick: dict[str, Any] = {
        "wallet_mono": 0.0,
        "open_journal": open_journal_last,
        "exit_runtimes": position_runtimes,
    }

    async def chain_refresh_loop() -> None:
        while not stop.is_set():
            try:
                await chain.refresh_instruments()
                await chain.refresh_quotes()
                now_chain = dt.datetime.now(dt.UTC).replace(tzinfo=None)
                await iv_history.record_from_chain(chain, underlying_marks, now_chain)
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
                    mp = (
                        msg.get("spot_price")
                        or msg.get("underlying_mark")
                        or msg.get("close")
                        or msg.get("last_price")
                    )
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
        nonlocal last_ist_trading_date, last_textfile
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

            last_ist_trading_date = maybe_roll_ist_trading_day(nav_tracker, now, last_ist_trading_date)

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
            peak_by_trade: dict[int, float] = {}
            quote_for: dict[str, QuoteSnapshot] = {}
            for _t, legs in open_rows:
                for leg in legs:
                    q = chain.get_quote(leg.symbol)
                    if q is not None:
                        quote_for[leg.symbol] = q
            iv_percentiles = await prefetch_iv_for_strategies(
                iv_history,
                chain,
                underlying_marks,
                strategy_cfgs,
                now=now,
            )
            market = MarketState(
                now=now,
                chain=chain,
                candles_by_tf=candles_by_tf,
                underlying_marks=dict(underlying_marks),
                quote_for=quote_for,
                usd_inr_rate=usd_inr,
                iv_percentiles=iv_percentiles,
            )
            open_ids = {t.id for t, _ in open_rows}
            for tid in list(position_runtimes.keys()):
                if tid not in open_ids:
                    position_runtimes.pop(tid)

            positions = [_trade_to_position_state(t, legs, chain) for t, legs in open_rows]

            for pos in positions:
                trade = next((t for t, _ in open_rows if t.id == pos.trade_id), None)
                legs = next((ls for t, ls in open_rows if t.id == pos.trade_id), [])
                if trade is None:
                    continue
                runtime = position_runtimes.get(pos.trade_id)
                if runtime is None:
                    runtime = PositionRuntime(position=pos)
                    position_runtimes[pos.trade_id] = runtime
                else:
                    runtime.position = pos
                directives = exit_engine.step(runtime, market)
                peak = max(
                    runtime.peak_pnl_inr,
                    float(runtime.position.peak_pnl_inr or 0.0),
                )
                peak_by_trade[trade.id] = peak

                for directive in directives:
                    if directive.kind == ExitKind.CLOSE and directive.trigger is not None:
                        wallet_exit: dict[str, Any] | None = None
                        if api_ok:
                            try:
                                wallet_exit = await fetch_wallet_snapshot(rest)
                            except DeltaRestError as exc:
                                logger.debug("wallet at exit: {}", exc)
                        ind_exit = indicator_snapshot_for_trade(trade, market)
                        await _persist_exit(
                            db=db,
                            executor=executor,
                            trade=trade,
                            legs=legs,
                            trigger=directive.trigger,
                            journal=journal,
                            metrics=metrics,
                            nav=nav_tracker,
                            wallet_at_exit=wallet_exit,
                            indicator_at_exit=ind_exit,
                            quote_for=quote_for,
                            chain=chain,
                        )
                        _apply_directional_exit_cooldown(registry, trade, now=now)
                        position_runtimes.pop(trade.id, None)
                    elif directive.kind == ExitKind.UPDATE_STOP and directive.new_stop_price is not None:
                        trail = TrailAction(new_stop_price=directive.new_stop_price)
                        await _persist_trail_stop(
                            db=db,
                            executor=executor,
                            trade=trade,
                            legs=legs,
                            trail=trail,
                            journal=journal,
                            wallet_snapshot=wallet_snap,
                        )

            if open_rows:
                await refresh_all_open_trades(
                    db,
                    open_rows,
                    chain,
                    market,
                    wallet_snapshot=wallet_snap,
                    peak_pnl_by_trade=peak_by_trade,
                )

            disp = dispatcher.evaluate_all(market)
            records: list[dict[str, Any]] = list(disp.all_decisions)

            accounting = await _accounting_snapshot(db)
            portfolio_book: PortfolioGreeks | None = None
            if app_config.global_config.desk.enabled:
                portfolio_book = PortfolioGreeks.from_open_trades(open_rows, quote_for, chain=chain)
                if portfolio_book is not None:
                    delta_inr = 0.0
                    vega_inr = 0.0
                    for key, ug in portfolio_book.by_underlying.items():
                        try:
                            underlying = Underlying(key)
                        except ValueError:
                            continue
                        spot = underlying_marks.get(underlying)
                        if spot is None or spot <= 0:
                            continue
                        delta_inr += abs(ug.delta) * float(spot) * usd_inr
                        vega_inr += abs(ug.vega) * usd_inr
                    metrics.portfolio_delta_inr.set(delta_inr)
                    metrics.portfolio_vega_inr.set(vega_inr)
                for (underlying, bucket), iv_result in iv_percentiles.items():
                    if iv_result.percentile is not None:
                        metrics.iv_percentile.labels(
                            underlying=underlying.value,
                            expiry_bucket=bucket.value,
                        ).set(iv_result.percentile)
            for intent in disp.all_intents:
                sizing = risk.gate(
                    intent,
                    now_utc=now,
                    accounting=accounting,
                    portfolio_greeks=portfolio_book,
                    quote_for=quote_for,
                    chain=chain,
                    underlying_marks=underlying_marks,
                    usd_inr_rate=usd_inr,
                )
                records.append(_risk_record(intent, sizing))
                metrics.intents_total.labels(intent.strategy_id.value).inc()
                if sizing.approved:
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
                        mode=settings.mode.value,
                        journal=journal,
                        wallet_at_entry=entry_wallet,
                        quote_for=quote_for,
                        chain=chain,
                    )
                    metrics.trades_opened_total.labels(intent.strategy_id.value).inc()
                    accounting = await _accounting_snapshot(db)

            await sync_circuit_breaker_from_risk(
                db,
                nav_tracker,
                risk,
                runtime_dir=settings.runtime_dir,
                now_utc=now,
            )

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
