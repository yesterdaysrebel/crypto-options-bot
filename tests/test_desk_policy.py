"""Desk policy gates wired through RiskManager."""

from __future__ import annotations

import datetime as dt

from bot.config.models import DeskConfig, DirectionalConfig, GlobalConfig, StrategyId, Underlying
from bot.data.chain_cache import QuoteSnapshot
from bot.desk.portfolio_greeks import PortfolioGreeks, UnderlyingGreeks
from bot.risk import RiskDecision, RiskManager, TradeAccountingSnapshot
from bot.risk.caps import NavTracker
from bot.strategies.base import Intent, LegIntent


def _intent_with_quote() -> Intent:
    return Intent(
        strategy_id=StrategyId.DIRECTIONAL,
        underlying=Underlying.BTC,
        bucket="D1",  # type: ignore[arg-type]
        legs=[
            LegIntent(
                symbol="C-BTC-100000-180526",
                side="buy",
                option_type="call",
                strike=100_000.0,
                expiry=dt.datetime(2026, 5, 18, 17, 30),
            ),
        ],
        requested_lots=1,
        rationale="test",
        target_premium_inr=200.0,
    )


def _risk_manager(desk: DeskConfig) -> RiskManager:
    g = GlobalConfig.model_validate(
        {
            "nav_inr": 50000,
            "usd_inr_rate": 85.0,
            "desk": desk.model_dump(),
        }
    )
    scfg = {
        StrategyId.DIRECTIONAL: DirectionalConfig.model_validate(
            {
                "id": "directional",
                "enabled": True,
                "risk_weight": 0.60,
                "risk_per_trade_pct": 0.01,
                "max_lots_cap": 10,
            }
        ),
    }
    return RiskManager(
        global_config=g,
        nav_tracker=NavTracker(
            nav_now=50_000.0,
            nav_open_today=50_000.0,
            nav_open_week=50_000.0,
            peak_nav=50_000.0,
        ),
        strategy_configs=scfg,
    )


def test_desk_disabled_skips_gates() -> None:
    mgr = _risk_manager(DeskConfig(enabled=False))
    intent = _intent_with_quote()
    quotes = {
        "C-BTC-100000-180526": QuoteSnapshot(symbol="C-BTC-100000-180526", delta=0.5, open_interest=0.0),
    }
    result = mgr.gate(
        intent,
        now_utc=dt.datetime(2026, 5, 18, 6, 0, 0),
        accounting=TradeAccountingSnapshot(0, {}),
        quote_for=quotes,
    )
    assert result.approved


def test_low_open_interest_rejects() -> None:
    mgr = _risk_manager(DeskConfig(enabled=True, min_open_interest=100, strict=True))
    intent = _intent_with_quote()
    quotes = {
        "C-BTC-100000-180526": QuoteSnapshot(
            symbol="C-BTC-100000-180526",
            delta=0.5,
            open_interest=10.0,
        ),
    }
    result = mgr.gate(
        intent,
        now_utc=dt.datetime(2026, 5, 18, 10, 0, 0),
        accounting=TradeAccountingSnapshot(0, {}),
        quote_for=quotes,
    )
    assert result.decision == RiskDecision.LOW_OPEN_INTEREST


def test_portfolio_delta_limit_rejects() -> None:
    mgr = _risk_manager(DeskConfig(enabled=True, max_abs_net_delta_inr=1000.0))
    intent = _intent_with_quote()
    quotes = {
        "C-BTC-100000-180526": QuoteSnapshot(
            symbol="C-BTC-100000-180526",
            delta=0.5,
            open_interest=500.0,
        ),
    }
    book = PortfolioGreeks(
        delta=1.0,
        gamma=0.0,
        theta=0.0,
        vega=0.0,
        by_underlying={"BTC": UnderlyingGreeks(delta=1.0)},
    )
    result = mgr.gate(
        intent,
        now_utc=dt.datetime(2026, 5, 18, 10, 0, 0),
        accounting=TradeAccountingSnapshot(0, {}),
        portfolio_greeks=book,
        quote_for=quotes,
        underlying_marks={Underlying.BTC: 100_000.0},
        usd_inr_rate=85.0,
    )
    assert result.decision == RiskDecision.PORTFOLIO_DELTA_LIMIT
