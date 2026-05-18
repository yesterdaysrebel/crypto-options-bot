"""Top-level RiskManager: turns a strategy Intent into either a sized order plan
(`SizingResult`) or a rejection with a reason. Wraps:

- Trading-window check
- Three-tier loss caps (daily/weekly/lifetime)
- Per-strategy concurrency (max 1) and global concurrency (max 3)
- Position sizing (per-strategy rules)
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from bot.config.models import GlobalConfig, StrategyConfig, StrategyId, Underlying
from bot.data.chain_cache import ChainCache, QuoteSnapshot
from bot.desk.portfolio_greeks import PortfolioGreeks
from bot.risk.caps import CapStatus, DrawdownCaps, LossCapResult, NavTracker
from bot.risk.window import TradingWindow

if TYPE_CHECKING:
    from bot.desk.policy import DeskPolicy
    from bot.strategies.base import Intent


class RiskDecision(StrEnum):
    APPROVED = "approved"
    OUTSIDE_TRADING_WINDOW = "outside_trading_window"
    CIRCUIT_BREAKER = "circuit_breaker"
    DAILY_CAP_TRIPPED = "daily_cap_tripped"
    WEEKLY_CAP_TRIPPED = "weekly_cap_tripped"
    STRATEGY_MAX_CONCURRENT = "strategy_max_concurrent"
    GLOBAL_MAX_CONCURRENT = "global_max_concurrent"
    ZERO_LOTS_AFTER_FLOOR = "zero_lots_after_floor"
    PREMIUM_ABOVE_RISK_BUDGET = "premium_above_risk_budget"
    CONDOR_MAX_LOSS_ABOVE_BUDGET = "condor_max_loss_above_budget"
    STRANGLE_PREMIUM_ABOVE_RISK_BUDGET = "strangle_premium_above_risk_budget"
    STRATEGY_DISABLED = "strategy_disabled"
    LOW_OPEN_INTEREST = "low_open_interest"
    MISSING_GREEKS = "missing_greeks"
    PORTFOLIO_DELTA_LIMIT = "portfolio_delta_limit"
    PORTFOLIO_VEGA_LIMIT = "portfolio_vega_limit"


@dataclass(frozen=True)
class SizingResult:
    decision: RiskDecision
    intent: Intent
    sized_lots: int = 0
    risk_inr: float = 0.0
    notes: dict[str, float | str] = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.decision == RiskDecision.APPROVED


@dataclass(frozen=True)
class TradeAccountingSnapshot:
    """Outside-world view of open positions used by the risk module each tick."""

    open_count_total: int
    open_count_by_strategy: dict[StrategyId, int]


class RiskManager:
    def __init__(
        self,
        *,
        global_config: GlobalConfig,
        nav_tracker: NavTracker,
        strategy_configs: dict[StrategyId, StrategyConfig],
    ) -> None:
        self._cfg = global_config
        self._nav = nav_tracker
        self._strategies = strategy_configs
        self._window = TradingWindow(
            global_config.trading_window.start,
            global_config.trading_window.end,
            global_config.trading_window.force_close,
        )
        self._caps = DrawdownCaps(
            daily_loss_pct=global_config.risk_caps.daily_loss_pct,
            weekly_loss_pct=global_config.risk_caps.weekly_loss_pct,
            lifetime_dd_pct=global_config.risk_caps.lifetime_dd_pct,
        )
        self._desk: DeskPolicy | None = None
        if global_config.desk.enabled:
            from bot.desk.policy import DeskPolicy as DeskPolicyCls

            self._desk = DeskPolicyCls(global_config.desk)

    @property
    def nav_tracker(self) -> NavTracker:
        return self._nav

    @property
    def window(self) -> TradingWindow:
        return self._window

    @property
    def caps(self) -> DrawdownCaps:
        return self._caps

    def evaluate_caps(self) -> LossCapResult:
        return self._caps.evaluate(
            nav_now=self._nav.nav_now,
            nav_open_today=self._nav.nav_open_today,
            nav_open_week=self._nav.nav_open_week,
            peak_nav=self._nav.peak_nav,
            circuit_breaker_tripped=self._nav.circuit_breaker_tripped,
        )

    def gate(
        self,
        intent: Intent,
        *,
        now_utc: dt.datetime,
        accounting: TradeAccountingSnapshot,
        portfolio_greeks: PortfolioGreeks | None = None,
        quote_for: Mapping[str, QuoteSnapshot] | None = None,
        chain: ChainCache | None = None,
        underlying_marks: Mapping[Underlying, float] | None = None,
        usd_inr_rate: float = 1.0,
    ) -> SizingResult:
        if not self._window.is_open(now_utc):
            return SizingResult(decision=RiskDecision.OUTSIDE_TRADING_WINDOW, intent=intent)

        cap_status = self.evaluate_caps()
        decision = _cap_to_decision(cap_status.status)
        if decision != RiskDecision.APPROVED:
            return SizingResult(
                decision=decision,
                intent=intent,
                notes={
                    "daily_pnl_pct": cap_status.daily_pnl_pct,
                    "weekly_pnl_pct": cap_status.weekly_pnl_pct,
                    "dd_pct": cap_status.drawdown_from_peak_pct,
                },
            )

        strategy_cfg = self._strategies.get(intent.strategy_id)
        if strategy_cfg is None or not strategy_cfg.enabled:
            return SizingResult(decision=RiskDecision.STRATEGY_DISABLED, intent=intent)

        if accounting.open_count_total >= self._cfg.concurrency.max_total:
            return SizingResult(decision=RiskDecision.GLOBAL_MAX_CONCURRENT, intent=intent)
        strategy_open = accounting.open_count_by_strategy.get(intent.strategy_id, 0)
        if strategy_open >= self._cfg.concurrency.max_per_strategy:
            return SizingResult(decision=RiskDecision.STRATEGY_MAX_CONCURRENT, intent=intent)

        if self._desk is not None:
            desk_notes: dict[str, Any] = {}
            if portfolio_greeks is not None and underlying_marks is not None:
                reason, pnotes = self._desk.check_portfolio(
                    portfolio_greeks,
                    underlying_marks,
                    usd_inr_rate=usd_inr_rate,
                )
                desk_notes.update(pnotes)
                if reason is not None:
                    return SizingResult(
                        decision=RiskDecision(reason),
                        intent=intent,
                        notes=desk_notes,
                    )
            if quote_for is not None:
                reason, inotes = self._desk.check_intent(intent, quote_for, chain)
                desk_notes.update(inotes)
                if reason is not None:
                    return SizingResult(
                        decision=RiskDecision(reason),
                        intent=intent,
                        notes=desk_notes,
                    )

        return self._size_intent(
            intent,
            strategy_cfg,
            quote_for=quote_for,
            chain=chain,
            usd_inr_rate=usd_inr_rate,
        )

    def _size_intent(
        self,
        intent: Intent,
        strategy_cfg: StrategyConfig,
        *,
        quote_for: Mapping[str, QuoteSnapshot] | None = None,
        chain: ChainCache | None = None,
        usd_inr_rate: float = 1.0,
    ) -> SizingResult:
        risk_budget_inr = self._nav.nav_now * strategy_cfg.risk_weight * strategy_cfg.risk_per_trade_pct
        sized: int
        notes: dict[str, float | str]

        if intent.strategy_id == StrategyId.DIRECTIONAL:
            if intent.target_premium_inr is None or intent.target_premium_inr <= 0:
                return SizingResult(
                    decision=RiskDecision.PREMIUM_ABOVE_RISK_BUDGET,
                    intent=intent,
                    notes={"reason": "missing_premium"},
                )
            per_lot_premium_inr = intent.target_premium_inr
            raw_lots = math.floor(risk_budget_inr / per_lot_premium_inr)
            sized = min(raw_lots, intent.requested_lots, strategy_cfg.max_lots_cap)
            if sized < 1:
                return SizingResult(
                    decision=RiskDecision.ZERO_LOTS_AFTER_FLOOR,
                    intent=intent,
                    notes={"risk_budget_inr": risk_budget_inr, "per_lot": per_lot_premium_inr},
                )
            risk_inr = sized * per_lot_premium_inr
            notes = {"risk_budget_inr": risk_budget_inr, "per_lot": per_lot_premium_inr}

        elif intent.strategy_id == StrategyId.IRON_CONDOR:
            if intent.target_max_loss_inr is None or intent.target_max_loss_inr <= 0:
                return SizingResult(
                    decision=RiskDecision.CONDOR_MAX_LOSS_ABOVE_BUDGET,
                    intent=intent,
                    notes={"reason": "missing_max_loss"},
                )
            per_lot_max_loss = intent.target_max_loss_inr
            raw_lots = math.floor(risk_budget_inr / per_lot_max_loss)
            sized = min(raw_lots, intent.requested_lots, strategy_cfg.max_lots_cap)
            if sized < 1:
                return SizingResult(
                    decision=RiskDecision.CONDOR_MAX_LOSS_ABOVE_BUDGET,
                    intent=intent,
                    notes={"risk_budget_inr": risk_budget_inr, "per_lot_max_loss": per_lot_max_loss},
                )
            risk_inr = sized * per_lot_max_loss
            notes = {"risk_budget_inr": risk_budget_inr, "per_lot_max_loss": per_lot_max_loss}

        elif intent.strategy_id == StrategyId.VOL_STRANGLE:
            if intent.target_premium_inr is None or intent.target_premium_inr <= 0:
                return SizingResult(
                    decision=RiskDecision.STRANGLE_PREMIUM_ABOVE_RISK_BUDGET,
                    intent=intent,
                    notes={"reason": "missing_premium"},
                )
            per_lot_premium = intent.target_premium_inr
            raw_lots = math.floor(risk_budget_inr / per_lot_premium)
            sized = min(raw_lots, intent.requested_lots, strategy_cfg.max_lots_cap)
            if sized < 1:
                return SizingResult(
                    decision=RiskDecision.STRANGLE_PREMIUM_ABOVE_RISK_BUDGET,
                    intent=intent,
                    notes={"risk_budget_inr": risk_budget_inr, "per_lot_premium": per_lot_premium},
                )
            risk_inr = sized * per_lot_premium
            notes = {"risk_budget_inr": risk_budget_inr, "per_lot_premium": per_lot_premium}

        desk_cfg = self._cfg.desk
        if desk_cfg.enabled and quote_for is not None and sized >= 1:
            from bot.desk.greek_sizing import cap_lots_by_greeks

            capped, gnotes = cap_lots_by_greeks(
                intent,
                sized,
                quote_for,
                chain,
                max_vega_inr=desk_cfg.max_vega_per_trade_inr,
                max_gamma_inr=desk_cfg.max_gamma_per_trade_inr,
                usd_inr_rate=usd_inr_rate,
            )
            notes.update({k: v for k, v in gnotes.items() if isinstance(v, (int, float, str))})
            if capped < sized:
                sized = capped
                if intent.strategy_id == StrategyId.DIRECTIONAL:
                    risk_inr = sized * float(intent.target_premium_inr or 0)
                elif intent.strategy_id == StrategyId.IRON_CONDOR:
                    risk_inr = sized * float(intent.target_max_loss_inr or 0)
                elif intent.strategy_id == StrategyId.VOL_STRANGLE:
                    risk_inr = sized * float(intent.target_premium_inr or 0)

        if sized < 1:
            return SizingResult(
                decision=RiskDecision.ZERO_LOTS_AFTER_FLOOR,
                intent=intent,
                notes=notes,
            )

        return SizingResult(
            decision=RiskDecision.APPROVED,
            intent=intent,
            sized_lots=sized,
            risk_inr=risk_inr,
            notes=notes,
        )


def _cap_to_decision(status: CapStatus) -> RiskDecision:
    return {
        CapStatus.OK: RiskDecision.APPROVED,
        CapStatus.DAILY_TRIPPED: RiskDecision.DAILY_CAP_TRIPPED,
        CapStatus.WEEKLY_TRIPPED: RiskDecision.WEEKLY_CAP_TRIPPED,
        CapStatus.CIRCUIT_BREAKER: RiskDecision.CIRCUIT_BREAKER,
    }[status]
