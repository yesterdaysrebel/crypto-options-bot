"""Pydantic models for global + per-strategy YAML configuration."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrategyId(StrEnum):
    DIRECTIONAL = "directional"
    IRON_CONDOR = "iron_condor"
    VOL_STRANGLE = "vol_strangle"


class Underlying(StrEnum):
    BTC = "BTC"
    ETH = "ETH"


class Timeframe(StrEnum):
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"


class ExpiryBucket(StrEnum):
    D1 = "D1"
    D2 = "D2"
    W1 = "W1"
    W2 = "W2"
    W3 = "W3"


class StrikeMode(StrEnum):
    ATM = "ATM"
    OTM_PLUS_ONE = "OTM_PLUS_ONE"


def _ist_time(value: str) -> dt.time:
    return dt.time.fromisoformat(value)


class RiskCapsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_loss_pct: Annotated[float, Field(gt=0, lt=1)] = 0.03
    weekly_loss_pct: Annotated[float, Field(gt=0, lt=1)] = 0.06
    lifetime_dd_pct: Annotated[float, Field(gt=0, lt=1)] = 0.15


class TradingWindowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_ist: str = "09:00"
    end_ist: str = "22:00"
    expiry_force_close_ist: str = "16:45"

    @field_validator("start_ist", "end_ist", "expiry_force_close_ist")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        _ist_time(v)
        return v

    @property
    def start(self) -> dt.time:
        return _ist_time(self.start_ist)

    @property
    def end(self) -> dt.time:
        return _ist_time(self.end_ist)

    @property
    def force_close(self) -> dt.time:
        return _ist_time(self.expiry_force_close_ist)


class ConcurrencyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_total: Annotated[int, Field(ge=1)] = 3
    max_per_strategy: Annotated[int, Field(ge=1)] = 1


class ExecutionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spread_filter_max_pct: Annotated[float, Field(gt=0, lt=1)] = 0.08
    maker_limit_timeout_seconds: Annotated[float, Field(gt=0)] = 30.0
    slip_bps_directional: Annotated[int, Field(ge=0, le=1000)] = 50
    slip_bps_strangle: Annotated[int, Field(ge=0, le=1000)] = 50
    slip_bps_condor: Annotated[int, Field(ge=0, le=1000)] = 100
    trail_update_throttle_seconds: Annotated[float, Field(gt=0)] = 5.0


class DeskConfig(BaseModel):
    """Desk-style gates (IV/OI/greeks/portfolio limits). Off by default until tuned in dry-run."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    strict: bool = False
    min_open_interest: Annotated[float, Field(ge=0)] = 0.0
    greeks_required: bool = True
    max_abs_net_delta_inr: Annotated[float, Field(gt=0)] | None = None
    max_abs_net_vega_inr: Annotated[float, Field(gt=0)] | None = None
    max_vega_per_trade_inr: Annotated[float, Field(gt=0)] | None = None
    max_gamma_per_trade_inr: Annotated[float, Field(gt=0)] | None = None
    iv_history_min_snapshots: Annotated[int, Field(ge=1)] = 20
    go_live_min_entry_iv_pct: Annotated[float, Field(gt=0, le=1)] = 0.80
    go_live_entry_iv_lookback_trades: Annotated[int, Field(ge=1)] = 20


class GlobalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nav_inr: Annotated[float, Field(gt=0)] = 50000.0
    usd_inr_rate: Annotated[float, Field(gt=0)] = 85.0
    risk_caps: RiskCapsConfig = RiskCapsConfig()
    trading_window: TradingWindowConfig = TradingWindowConfig()
    concurrency: ConcurrencyConfig = ConcurrencyConfig()
    execution: ExecutionConfig = ExecutionConfig()
    desk: DeskConfig = DeskConfig()


class BaseStrategyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StrategyId
    enabled: bool = True
    enabled_live: bool = False
    risk_weight: Annotated[float, Field(gt=0, le=1.0)]
    risk_per_trade_pct: Annotated[float, Field(gt=0, lt=1.0)]
    max_lots_cap: Annotated[int, Field(ge=1)]
    underlyings: list[Underlying] = Field(default_factory=lambda: [Underlying.BTC, Underlying.ETH])


class DirectionalEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeframe: Timeframe = Timeframe.M15
    ema_fast: int = 9
    ema_slow: int = 21
    atr_period: int = 14
    prior_bars: int = 4
    breakout_atr_mult: float = 0.25
    cooldown_minutes_after_underlying_stop: Annotated[float, Field(ge=0)] = 45.0


class DirectionalExpiry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prefer: ExpiryBucket = ExpiryBucket.D1
    fallback: ExpiryBucket = ExpiryBucket.D2
    min_dte_hours: Annotated[float, Field(gt=0)] = 2.0
    d1_dte_threshold_hours: Annotated[float, Field(gt=0)] = 4.0


class DirectionalStrike(BaseModel):
    model_config = ConfigDict(extra="forbid")
    primary: StrikeMode = StrikeMode.ATM
    fallback: StrikeMode = StrikeMode.OTM_PLUS_ONE


class DirectionalDeskConfig(BaseModel):
    """Per-strategy desk overrides (IV regime, optional delta-target entry)."""

    model_config = ConfigDict(extra="forbid")

    max_iv_percentile_long: Annotated[float, Field(gt=0, le=1)] | None = None
    min_iv_percentile_long: Annotated[float, Field(gt=0, le=1)] | None = None
    prefer_delta_strike: Annotated[float, Field(gt=0, lt=1)] | None = None
    max_abs_delta_move: Annotated[float, Field(gt=0, lt=1)] | None = None
    max_abs_gamma_shock: Annotated[float, Field(gt=0)] | None = None


class StrategyLegDeskConfig(BaseModel):
    """Liquidity / greek gates for multi-leg entries (condor, strangle)."""

    model_config = ConfigDict(extra="forbid")

    min_open_interest: Annotated[float, Field(ge=0)] = 0.0
    greeks_required: bool = True


class DirectionalExits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_r: Annotated[float, Field(gt=0)] = 2.0
    premium_drawdown_pct: Annotated[float, Field(gt=0, lt=1)] = 0.50
    underlying_atr_mult_stop: Annotated[float, Field(gt=0)] = 1.0
    trail_breakeven_at_r: Annotated[float, Field(gt=0)] = 1.0
    trail_chandelier_atr_mult: Annotated[float, Field(gt=0)] = 2.0
    force_close_hours_before_expiry: Annotated[float, Field(gt=0)] = 2.0


class DirectionalConfig(BaseStrategyConfig):
    id: Literal[StrategyId.DIRECTIONAL] = StrategyId.DIRECTIONAL
    entry: DirectionalEntry = DirectionalEntry()
    expiry: DirectionalExpiry = DirectionalExpiry()
    strike: DirectionalStrike = DirectionalStrike()
    desk: DirectionalDeskConfig = DirectionalDeskConfig()
    exits: DirectionalExits = DirectionalExits()


class CondorEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weekday: Literal["friday"] = "friday"
    open_time_ist: str = "09:30"
    monthly_cooldown_on_max_loss: bool = True

    @field_validator("open_time_ist")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        _ist_time(v)
        return v

    @property
    def open_time(self) -> dt.time:
        return _ist_time(self.open_time_ist)


class CondorWings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    short_delta_min: Annotated[float, Field(gt=0, lt=1)] = 0.15
    short_delta_max: Annotated[float, Field(gt=0, lt=1)] = 0.25
    short_delta_target: Annotated[float, Field(gt=0, lt=1)] = 0.20
    long_delta_min: Annotated[float, Field(gt=0, lt=1)] = 0.05
    long_delta_max: Annotated[float, Field(gt=0, lt=1)] = 0.10
    long_delta_target: Annotated[float, Field(gt=0, lt=1)] = 0.075


class CondorCredit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_credit_pct_of_width: Annotated[float, Field(gt=0, lt=1)] = 0.25


class CondorExits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profit_take_pct_of_credit: Annotated[float, Field(gt=0, lt=1)] = 0.50
    stop_loss_x_credit: Annotated[float, Field(gt=0)] = 2.0
    force_close_days_before_expiry: Annotated[int, Field(ge=0, le=7)] = 2
    tested_side_cut: bool = True


class IronCondorConfig(BaseStrategyConfig):
    id: Literal[StrategyId.IRON_CONDOR] = StrategyId.IRON_CONDOR
    entry: CondorEntry = CondorEntry()
    wings: CondorWings = CondorWings()
    credit: CondorCredit = CondorCredit()
    desk: StrategyLegDeskConfig = StrategyLegDeskConfig()
    exits: CondorExits = CondorExits()


class VolStrangleSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeframe_short: Timeframe = Timeframe.M15
    timeframe_long: Timeframe = Timeframe.H1
    atr_period: int = 14
    atr_percentile_max: Annotated[float, Field(gt=0, le=1)] = 0.30
    atr_percentile_lookback: int = 100
    bbwidth_period: int = 20
    bbwidth_std: float = 2.0
    bbwidth_lookback_days: int = 180
    range_compression_bars: int = 8
    range_compression_ratio: Annotated[float, Field(gt=0, lt=1)] = 0.5
    range_compression_lookback_days: int = 14
    anti_revenge_hours: Annotated[int, Field(ge=0)] = 24


class VolStrangleExpiry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prefer: ExpiryBucket = ExpiryBucket.D2
    fallback: ExpiryBucket = ExpiryBucket.W1


class VolStrangleStrike(BaseModel):
    model_config = ConfigDict(extra="forbid")
    call_delta_min: Annotated[float, Field(gt=0, lt=1)] = 0.20
    call_delta_max: Annotated[float, Field(gt=0, lt=1)] = 0.30
    call_delta_target: Annotated[float, Field(gt=0, lt=1)] = 0.25
    put_delta_min: Annotated[float, Field(gt=0, lt=1)] = 0.20
    put_delta_max: Annotated[float, Field(gt=0, lt=1)] = 0.30
    put_delta_target: Annotated[float, Field(gt=0, lt=1)] = 0.25


class VolStrangleExits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profit_take_pct_of_premium: Annotated[float, Field(gt=0, lt=1)] = 0.50
    stop_loss_pct_of_premium: Annotated[float, Field(gt=0, lt=1)] = 0.50
    force_close_hours_before_expiry: Annotated[float, Field(gt=0)] = 4.0


class VolStrangleConfig(BaseStrategyConfig):
    id: Literal[StrategyId.VOL_STRANGLE] = StrategyId.VOL_STRANGLE
    setup: VolStrangleSetup = VolStrangleSetup()
    expiry: VolStrangleExpiry = VolStrangleExpiry()
    strike: VolStrangleStrike = VolStrangleStrike()
    desk: StrategyLegDeskConfig = StrategyLegDeskConfig()
    exits: VolStrangleExits = VolStrangleExits()


StrategyConfig = DirectionalConfig | IronCondorConfig | VolStrangleConfig
