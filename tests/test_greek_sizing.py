"""Per-trade vega/gamma lot caps (desk greek sizing)."""

from __future__ import annotations

import datetime as dt

from bot.config.models import DeskConfig, GlobalConfig, StrategyId, Underlying, VolStrangleConfig
from bot.data.chain_cache import QuoteSnapshot
from bot.desk.greek_sizing import cap_lots_by_greeks
from bot.risk import RiskManager, TradeAccountingSnapshot
from bot.risk.caps import NavTracker
from bot.strategies.base import Intent, LegIntent


def _strangle_intent() -> Intent:
    return Intent(
        strategy_id=StrategyId.VOL_STRANGLE,
        underlying=Underlying.BTC,
        bucket="D1",  # type: ignore[arg-type]
        legs=[
            LegIntent(
                symbol="C-BTC-100000-180526",
                side="sell",
                option_type="call",
                strike=100_000.0,
                expiry=dt.datetime(2026, 5, 18, 17, 30),
            ),
            LegIntent(
                symbol="P-BTC-100000-180526",
                side="sell",
                option_type="put",
                strike=100_000.0,
                expiry=dt.datetime(2026, 5, 18, 17, 30),
            ),
        ],
        requested_lots=5,
        rationale="test",
        target_premium_inr=50.0,
    )


def test_cap_lots_by_vega_reduces_lots() -> None:
    intent = _strangle_intent()
    quotes = {
        sym: QuoteSnapshot(symbol=sym, vega=10.0, delta=0.3, open_interest=500.0)
        for sym in ("C-BTC-100000-180526", "P-BTC-100000-180526")
    }
    capped, notes = cap_lots_by_greeks(
        intent,
        5,
        quotes,
        None,
        max_vega_inr=500.0,
        max_gamma_inr=None,
        usd_inr_rate=85.0,
    )
    # vega per lot ≈ 2 legs * 10 * 1 * 85 = 1700 INR → floor(500/1700) = 0 → min 1
    assert capped == 1
    assert notes.get("greek_cap_applied") is True


def test_risk_manager_applies_greek_cap_when_desk_enabled() -> None:
    g = GlobalConfig.model_validate(
        {
            "nav_inr": 50000,
            "usd_inr_rate": 85.0,
            "desk": DeskConfig(
                enabled=True,
                min_open_interest=0,
                greeks_required=False,
                max_vega_per_trade_inr=500.0,
            ).model_dump(),
        }
    )
    scfg = {
        StrategyId.VOL_STRANGLE: VolStrangleConfig.model_validate(
            {
                "id": "vol_strangle",
                "enabled": True,
                "risk_weight": 0.15,
                "risk_per_trade_pct": 0.015,
                "max_lots_cap": 10,
            }
        ),
    }
    mgr = RiskManager(
        global_config=g,
        nav_tracker=NavTracker(
            nav_now=50_000.0,
            nav_open_today=50_000.0,
            nav_open_week=50_000.0,
            peak_nav=50_000.0,
        ),
        strategy_configs=scfg,
    )
    intent = _strangle_intent()
    quotes = {
        sym: QuoteSnapshot(symbol=sym, vega=10.0, delta=0.3, open_interest=500.0)
        for sym in ("C-BTC-100000-180526", "P-BTC-100000-180526")
    }
    result = mgr.gate(
        intent,
        now_utc=dt.datetime(2026, 5, 18, 10, 0, 0),
        accounting=TradeAccountingSnapshot(0, {}),
        quote_for=quotes,
        usd_inr_rate=85.0,
    )
    assert result.approved
    assert result.sized_lots == 1
    assert result.notes.get("lots_before_greek_cap") == 2.0
