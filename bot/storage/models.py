"""SQLAlchemy 2.0 ORM models. All trade-bearing tables are strategy_id-keyed.

Schema overview (one row per ...):
    instruments       - listed product, refreshed every 5min
    market_snapshots  - OHLCV bar (15m for indices, 1m for spot, ticks only when traded)
    decisions         - every strategy evaluation tick OR position-manage tick
    signals           - when a strategy WOULD trade (intent before order send)
    orders            - every order submit / ack / fill / cancel / reject (lifecycle)
    legs              - per leg of a logical position (4 for condor, 2 for strangle, 1 for directional)
    trades            - one logical position (entry + exit, PnL split)
    daily_pnl         - per-strategy day roll-up, computed by nightly aggregator
    nav_history       - end-of-day NAV + rolling peak (drives lifetime DD circuit breaker)
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[type, Any]] = {dict[str, Any]: JSON}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class DecisionKind(StrEnum):
    EVALUATE = "evaluate"  # filter check, would-trade decision
    MANAGE = "manage"  # ongoing-position management tick
    RISK = "risk"  # risk module checked caps / window / concurrency


class DecisionReason(StrEnum):
    PASSED = "passed"
    FILTER_FAILED = "filter_failed"
    SPREAD_TOO_WIDE = "spread_too_wide"
    PREMIUM_ABOVE_RISK_BUDGET = "premium_above_risk_budget"
    ZERO_LOTS_AFTER_FLOOR = "zero_lots_after_floor"
    CREDIT_TOO_THIN = "credit_too_thin"
    CONDOR_MAX_LOSS_ABOVE_BUDGET = "condor_max_loss_above_budget"
    CONDOR_DELTA_BAND_UNFILLABLE = "condor_delta_band_unfillable"
    STRANGLE_PREMIUM_ABOVE_RISK_BUDGET = "strangle_premium_above_risk_budget"
    NO_ACCEPTABLE_STRIKE = "no_acceptable_strike"
    OUTSIDE_TRADING_WINDOW = "outside_trading_window"
    STRATEGY_MAX_CONCURRENT = "strategy_max_concurrent"
    GLOBAL_MAX_CONCURRENT = "global_max_concurrent"
    DAILY_CAP_TRIPPED = "daily_cap_tripped"
    WEEKLY_CAP_TRIPPED = "weekly_cap_tripped"
    CIRCUIT_BREAKER = "circuit_breaker"
    COOLDOWN_ACTIVE = "cooldown_active"
    ANTI_REVENGE_BLOCK = "anti_revenge_block"
    DTE_TOO_SHORT = "dte_too_short"
    STRATEGY_DISABLED = "strategy_disabled"
    OTHER = "other"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    LIMIT_POST_ONLY = "limit_post_only"
    LIMIT_IOC = "limit_ioc"
    MARKET = "market"
    STOP_MARKET = "stop_market"
    STOP_LIMIT = "stop_limit"


class OrderState(StrEnum):
    PENDING = "pending"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


class TradeStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    ERRORED = "errored"


class LegStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class ExitReason(StrEnum):
    TARGET = "target"
    PREMIUM_STOP = "premium_stop"
    UNDERLYING_STOP = "underlying_stop"
    TRAIL_CHANDELIER = "trail_chandelier"
    TRAIL_BREAKEVEN = "trail_breakeven"
    TIME_STOP = "time_stop"
    FORCE_CLOSE_EXPIRY = "force_close_expiry"
    TESTED_SIDE_CUT = "tested_side_cut"
    KILL_SWITCH = "kill_switch"
    CIRCUIT_BREAKER = "circuit_breaker"
    MANUAL = "manual"


class Instrument(Base):
    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    underlying: Mapped[str] = mapped_column(String(16), index=True)
    contract_type: Mapped[str] = mapped_column(String(32))
    option_type: Mapped[str | None] = mapped_column(String(8), nullable=True)
    strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    expiry: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    lot_size: Mapped[float] = mapped_column(Float, default=1.0)
    tick_size: Mapped[float] = mapped_column(Float, default=0.5)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    refreshed_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    __table_args__ = (Index("ix_market_snapshots_sym_ts", "symbol", "timeframe", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    timeframe: Mapped[str] = mapped_column(String(8))  # 1m, 15m, 1h, tick
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    iv: Mapped[float | None] = mapped_column(Float, nullable=True)
    delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    gamma: Mapped[float | None] = mapped_column(Float, nullable=True)
    theta: Mapped[float | None] = mapped_column(Float, nullable=True)
    vega: Mapped[float | None] = mapped_column(Float, nullable=True)
    rho: Mapped[float | None] = mapped_column(Float, nullable=True)


class Decision(Base):
    __tablename__ = "decisions"
    __table_args__ = (Index("ix_decisions_strategy_ts", "strategy_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    strategy_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    underlying: Mapped[str | None] = mapped_column(String(16), nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(64), default=DecisionReason.OTHER.value)
    feature_vector: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    strategy_id: Mapped[str] = mapped_column(String(32), index=True)
    underlying: Mapped[str] = mapped_column(String(16))
    side: Mapped[str | None] = mapped_column(String(8), nullable=True)
    intended_symbol: Mapped[str] = mapped_column(String(64))
    intended_expiry: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    intended_strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    intended_lots: Mapped[int] = mapped_column(Integer, default=1)
    intended_premium_inr: Mapped[float | None] = mapped_column(Float, nullable=True)
    feature_vector: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(String(32), index=True)
    underlying: Mapped[str] = mapped_column(String(16), index=True)
    expiry: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    entry_ts: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    exit_ts: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=TradeStatus.OPEN.value, index=True)
    mode: Mapped[str] = mapped_column(String(8), default="dry")
    lots: Mapped[int] = mapped_column(Integer, default=1)
    premium_paid_inr: Mapped[float | None] = mapped_column(Float, nullable=True)
    credit_received_inr: Mapped[float | None] = mapped_column(Float, nullable=True)
    realised_pnl_inr: Mapped[float | None] = mapped_column(Float, nullable=True)
    delta_pnl_inr: Mapped[float | None] = mapped_column(Float, nullable=True)
    theta_pnl_inr: Mapped[float | None] = mapped_column(Float, nullable=True)
    fees_inr: Mapped[float | None] = mapped_column(Float, nullable=True)
    slippage_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entry_iv: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_iv: Mapped[float | None] = mapped_column(Float, nullable=True)
    peak_pnl_inr: Mapped[float | None] = mapped_column(Float, nullable=True)
    trough_pnl_inr: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    notes: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    legs: Mapped[list[Leg]] = relationship("Leg", back_populates="trade", cascade="all, delete-orphan")


class Leg(Base):
    __tablename__ = "legs"
    __table_args__ = (Index("ix_legs_trade_idx", "trade_id", "leg_idx"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id", ondelete="CASCADE"), index=True)
    strategy_id: Mapped[str] = mapped_column(String(32), index=True)
    leg_idx: Mapped[int] = mapped_column(Integer)
    symbol: Mapped[str] = mapped_column(String(64))
    option_type: Mapped[str | None] = mapped_column(String(8), nullable=True)
    strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    side: Mapped[str] = mapped_column(String(8))
    lots: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_inr: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=LegStatus.OPEN.value)

    trade: Mapped[Trade] = relationship("Trade", back_populates="legs")


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_orders_client_order_id"),
        Index("ix_orders_strategy_ts", "strategy_id", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    strategy_id: Mapped[str] = mapped_column(String(32), index=True)
    trade_id: Mapped[int | None] = mapped_column(
        ForeignKey("trades.id", ondelete="SET NULL"), nullable=True, index=True
    )
    leg_idx: Mapped[int | None] = mapped_column(Integer, nullable=True)
    client_order_id: Mapped[str] = mapped_column(String(64), index=True)
    exchange_order_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(24))
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    qty: Mapped[float] = mapped_column(Float)
    filled_qty: Mapped[float] = mapped_column(Float, default=0.0)
    filled_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(16), default=OrderState.PENDING.value, index=True)
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class DailyPnl(Base):
    __tablename__ = "daily_pnl"
    __table_args__ = (UniqueConstraint("trading_date", "strategy_id", name="uq_daily_pnl_date_strategy"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trading_date: Mapped[dt.date] = mapped_column(Date, index=True)
    strategy_id: Mapped[str] = mapped_column(String(32), index=True)
    n_trades: Mapped[int] = mapped_column(Integer, default=0)
    n_wins: Mapped[int] = mapped_column(Integer, default=0)
    n_losses: Mapped[int] = mapped_column(Integer, default=0)
    gross_pnl_inr: Mapped[float] = mapped_column(Float, default=0.0)
    net_pnl_inr: Mapped[float] = mapped_column(Float, default=0.0)
    fees_inr: Mapped[float] = mapped_column(Float, default=0.0)
    delta_pnl_inr: Mapped[float] = mapped_column(Float, default=0.0)
    theta_pnl_inr: Mapped[float] = mapped_column(Float, default=0.0)
    avg_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    win_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_slippage_bps: Mapped[float | None] = mapped_column(Float, nullable=True)


class NavHistory(Base):
    __tablename__ = "nav_history"
    __table_args__ = (CheckConstraint("nav_inr >= 0", name="ck_nav_history_nav_nonneg"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trading_date: Mapped[dt.date] = mapped_column(Date, unique=True, index=True)
    nav_inr: Mapped[float] = mapped_column(Float)
    peak_nav_inr: Mapped[float] = mapped_column(Float)
    drawdown_from_peak_pct: Mapped[float] = mapped_column(Float, default=0.0)
    circuit_breaker_tripped: Mapped[bool] = mapped_column(Boolean, default=False)
