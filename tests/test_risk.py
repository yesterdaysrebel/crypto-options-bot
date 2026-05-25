"""Tests for the RiskManager.

AC for PR #12: at +0.1% NAV the manager approves; at -3% daily it returns DAILY_CAP_TRIPPED;
at -6% weekly: WEEKLY_CAP_TRIPPED; at -15% lifetime peak-to-trough: CIRCUIT_BREAKER and trips
the breaker. The window check rejects outside 9:00-22:00 IST. Concurrency caps respect
max_per_strategy=1 and max_total=3.
"""

from __future__ import annotations

import datetime as dt

import pytest

from bot.config.models import (
    CreditVerticalConfig,
    DirectionalConfig,
    GlobalConfig,
    LongStraddleConfig,
    StrategyId,
    Underlying,
)
from bot.risk import (
    CapStatus,
    DrawdownCaps,
    NavTracker,
    RiskDecision,
    RiskManager,
    TradeAccountingSnapshot,
    TradingWindow,
)
from bot.strategies.base import Intent, LegIntent


def _intent_directional(premium=200.0, lots=10) -> Intent:
    return Intent(
        strategy_id=StrategyId.DIRECTIONAL,
        underlying=Underlying.BTC,
        bucket="D1",  # type: ignore[arg-type]
        legs=[
            LegIntent(
                symbol="C-BTC-100000-130524",
                side="buy",
                option_type="call",
                strike=100000,
                expiry=dt.datetime(2026, 5, 13, 17, 30),
            ),
        ],
        requested_lots=lots,
        rationale="test",
        target_premium_inr=premium,
    )


def _intent_condor(max_loss=500.0, lots=3) -> Intent:
    return Intent(
        strategy_id=StrategyId.CREDIT_VERTICAL,
        underlying=Underlying.BTC,
        bucket="W1",  # type: ignore[arg-type]
        legs=[],
        requested_lots=lots,
        rationale="test",
        target_credit_inr=500.0,
        target_max_loss_inr=max_loss,
    )


def _intent_strangle(premium=200.0, lots=5) -> Intent:
    return Intent(
        strategy_id=StrategyId.LONG_STRADDLE,
        underlying=Underlying.BTC,
        bucket="D2",  # type: ignore[arg-type]
        legs=[],
        requested_lots=lots,
        rationale="test",
        target_premium_inr=premium,
    )


def _global_cfg() -> GlobalConfig:
    return GlobalConfig.model_validate({"nav_inr": 50000, "usd_inr_rate": 85})


def _strategy_configs() -> dict[StrategyId, object]:
    return {
        StrategyId.DIRECTIONAL: DirectionalConfig.model_validate(
            {
                "id": "directional",
                "enabled": True,
                "risk_weight": 0.60,
                "risk_per_trade_pct": 0.01,
                "max_lots_cap": 10,
            }
        ),
        StrategyId.CREDIT_VERTICAL: CreditVerticalConfig.model_validate(
            {
                "id": "credit_vertical",
                "enabled": True,
                "risk_weight": 0.25,
                "risk_per_trade_pct": 0.015,
                "max_lots_cap": 3,
            }
        ),
        StrategyId.LONG_STRADDLE: LongStraddleConfig.model_validate(
            {
                "id": "long_straddle",
                "enabled": True,
                "risk_weight": 0.15,
                "risk_per_trade_pct": 0.01,
                "max_lots_cap": 5,
            }
        ),
    }


def _empty_accounting() -> TradeAccountingSnapshot:
    return TradeAccountingSnapshot(open_count_total=0, open_count_by_strategy={})


def _now_in_window() -> dt.datetime:
    # 10:00 IST = 04:30 UTC
    return dt.datetime(2026, 5, 12, 4, 30, 0)


def _now_outside_window() -> dt.datetime:
    # 23:00 IST = 17:30 UTC
    return dt.datetime(2026, 5, 12, 17, 30, 0)


def _manager(
    *, nav_open_today=50000.0, nav_open_week=50000.0, nav_now=50000.0, peak_nav=50000.0, breaker=False
) -> RiskManager:
    tracker = NavTracker(
        nav_now=nav_now,
        nav_open_today=nav_open_today,
        nav_open_week=nav_open_week,
        peak_nav=peak_nav,
        circuit_breaker_tripped=breaker,
    )
    return RiskManager(global_config=_global_cfg(), nav_tracker=tracker, strategy_configs=_strategy_configs())


def test_window_open_at_10_ist() -> None:
    w = TradingWindow(dt.time(9, 0), dt.time(22, 0), dt.time(16, 45))
    assert w.is_open(_now_in_window())
    assert not w.is_open(_now_outside_window())


def test_caps_ok_when_pnl_small() -> None:
    caps = DrawdownCaps(daily_loss_pct=0.03, weekly_loss_pct=0.06, lifetime_dd_pct=0.15)
    out = caps.evaluate(
        nav_now=50050,
        nav_open_today=50000,
        nav_open_week=50000,
        peak_nav=50050,
        circuit_breaker_tripped=False,
    )
    assert out.status == CapStatus.OK


def test_caps_daily_trip() -> None:
    caps = DrawdownCaps(daily_loss_pct=0.03, weekly_loss_pct=0.06, lifetime_dd_pct=0.15)
    out = caps.evaluate(
        nav_now=48500,
        nav_open_today=50000,
        nav_open_week=50000,
        peak_nav=50000,
        circuit_breaker_tripped=False,
    )
    assert out.status == CapStatus.DAILY_TRIPPED


def test_caps_weekly_trip() -> None:
    caps = DrawdownCaps(daily_loss_pct=0.03, weekly_loss_pct=0.06, lifetime_dd_pct=0.15)
    out = caps.evaluate(
        nav_now=47000,
        nav_open_today=47200,
        nav_open_week=50000,
        peak_nav=50000,
        circuit_breaker_tripped=False,
    )
    assert out.status == CapStatus.WEEKLY_TRIPPED


def test_caps_circuit_breaker_dd() -> None:
    caps = DrawdownCaps(daily_loss_pct=0.03, weekly_loss_pct=0.06, lifetime_dd_pct=0.15)
    out = caps.evaluate(
        nav_now=42500,
        nav_open_today=42600,
        nav_open_week=42400,
        peak_nav=50000,
        circuit_breaker_tripped=False,
    )
    assert out.status == CapStatus.CIRCUIT_BREAKER


def test_caps_circuit_breaker_already_tripped() -> None:
    caps = DrawdownCaps(daily_loss_pct=0.03, weekly_loss_pct=0.06, lifetime_dd_pct=0.15)
    out = caps.evaluate(
        nav_now=50000, nav_open_today=50000, nav_open_week=50000, peak_nav=50000, circuit_breaker_tripped=True
    )
    assert out.status == CapStatus.CIRCUIT_BREAKER


def test_directional_sizing_btc_with_contract_value() -> None:
    """BTC mids look huge without lot_size; per-lot notional is mid x contract_value x FX."""
    cfg = DirectionalConfig.model_validate(
        {
            "id": "directional",
            "enabled": True,
            "risk_weight": 0.60,
            "risk_per_trade_pct": 0.01,
            "max_lots_cap": 20,
            "trade_premium_cap_usd": 50.0,
        }
    )
    mgr = RiskManager(
        global_config=_global_cfg(),
        nav_tracker=NavTracker(
            nav_now=67000.0,
            nav_open_today=67000.0,
            nav_open_week=67000.0,
            peak_nav=67000.0,
            circuit_breaker_tripped=False,
        ),
        strategy_configs={StrategyId.DIRECTIONAL: cfg},
    )
    per_lot = 80.0 * 0.001 * 85.0  # BTC-ish mid x contract_value x INR
    intent = _intent_directional(premium=per_lot, lots=20)
    result = mgr.gate(intent, now_utc=_now_in_window(), accounting=_empty_accounting(), usd_inr_rate=85.0)
    assert result.approved
    assert result.sized_lots == 20
    assert result.notes["risk_budget_inr"] == pytest.approx(50.0 * 85.0)


def test_directional_sizing_floor_lots() -> None:
    mgr = _manager()
    intent = _intent_directional(premium=400.0, lots=10)  # budget = 50k * 0.6 * 0.01 = 300; per_lot=400
    result = mgr.gate(intent, now_utc=_now_in_window(), accounting=_empty_accounting())
    assert result.decision == RiskDecision.ZERO_LOTS_AFTER_FLOOR


def test_live_mode_blocks_strategy_without_enabled_live() -> None:
    mgr = RiskManager(
        global_config=_global_cfg(),
        nav_tracker=NavTracker(
            nav_now=50000.0,
            nav_open_today=50000.0,
            nav_open_week=50000.0,
            peak_nav=50000.0,
            circuit_breaker_tripped=False,
        ),
        strategy_configs=_strategy_configs(),
        require_live_enabled=True,
    )
    intent = _intent_directional(premium=100.0, lots=10)
    result = mgr.gate(intent, now_utc=_now_in_window(), accounting=_empty_accounting())
    assert result.decision == RiskDecision.STRATEGY_NOT_LIVE_ENABLED


def test_directional_sizing_normal() -> None:
    mgr = _manager()
    intent = _intent_directional(premium=100.0, lots=10)
    result = mgr.gate(intent, now_utc=_now_in_window(), accounting=_empty_accounting())
    assert result.approved
    assert result.sized_lots == 3  # floor(300 / 100) = 3
    assert result.risk_inr == 300.0


def test_directional_sizing_caps_to_strategy_lots() -> None:
    mgr = _manager(nav_now=10_000_000.0, peak_nav=10_000_000.0)
    intent = _intent_directional(premium=100.0, lots=10)
    result = mgr.gate(intent, now_utc=_now_in_window(), accounting=_empty_accounting())
    assert result.approved
    assert result.sized_lots == 10  # capped by max_lots_cap


def test_condor_sizing_uses_max_loss() -> None:
    mgr = _manager()
    intent = _intent_condor(max_loss=100.0, lots=3)  # budget = 50k * 0.25 * 0.015 = 187.5; per_lot=100
    result = mgr.gate(intent, now_utc=_now_in_window(), accounting=_empty_accounting())
    assert result.approved
    assert result.sized_lots == 1


def test_strangle_sizing_uses_total_premium() -> None:
    mgr = _manager()
    intent = _intent_strangle(premium=20.0, lots=5)  # budget = 50k * 0.15 * 0.01 = 75; per_lot=20
    result = mgr.gate(intent, now_utc=_now_in_window(), accounting=_empty_accounting())
    assert result.approved
    assert result.sized_lots == 3  # floor(75 / 20) = 3


def test_outside_window_rejects() -> None:
    mgr = _manager()
    result = mgr.gate(_intent_directional(), now_utc=_now_outside_window(), accounting=_empty_accounting())
    assert result.decision == RiskDecision.OUTSIDE_TRADING_WINDOW


def test_circuit_breaker_rejects_all_intents() -> None:
    mgr = _manager(breaker=True)
    for intent in [_intent_directional(), _intent_condor(), _intent_strangle()]:
        result = mgr.gate(intent, now_utc=_now_in_window(), accounting=_empty_accounting())
        assert result.decision == RiskDecision.CIRCUIT_BREAKER


def test_per_strategy_max_concurrent_rejects() -> None:
    mgr = _manager()
    accounting = TradeAccountingSnapshot(
        open_count_total=1,
        open_count_by_strategy={StrategyId.DIRECTIONAL: 1},
    )
    result = mgr.gate(_intent_directional(), now_utc=_now_in_window(), accounting=accounting)
    assert result.decision == RiskDecision.STRATEGY_MAX_CONCURRENT


def test_global_max_concurrent_rejects() -> None:
    mgr = _manager()
    accounting = TradeAccountingSnapshot(
        open_count_total=3,
        open_count_by_strategy={
            StrategyId.DIRECTIONAL: 1,
            StrategyId.CREDIT_VERTICAL: 1,
            StrategyId.LONG_STRADDLE: 1,
        },
    )
    result = mgr.gate(_intent_directional(), now_utc=_now_in_window(), accounting=accounting)
    assert result.decision == RiskDecision.GLOBAL_MAX_CONCURRENT


def test_disabled_strategy_rejects() -> None:
    cfg = _global_cfg()
    scfg = {
        StrategyId.DIRECTIONAL: DirectionalConfig.model_validate(
            {
                "id": "directional",
                "enabled": False,
                "risk_weight": 0.60,
                "risk_per_trade_pct": 0.01,
                "max_lots_cap": 10,
            }
        ),
    }
    tracker = NavTracker(nav_now=50000, nav_open_today=50000, nav_open_week=50000, peak_nav=50000)
    mgr = RiskManager(global_config=cfg, nav_tracker=tracker, strategy_configs=scfg)  # type: ignore[arg-type]
    result = mgr.gate(_intent_directional(), now_utc=_now_in_window(), accounting=_empty_accounting())
    assert result.decision == RiskDecision.STRATEGY_DISABLED


def test_nav_tracker_peak_monotonic() -> None:
    t = NavTracker(nav_now=50000, nav_open_today=50000, nav_open_week=50000, peak_nav=50000)
    t.update_nav(51000)
    assert t.peak_nav == 51000
    t.update_nav(49000)
    assert t.peak_nav == 51000


def test_nav_tracker_roll_day_resets_today_only() -> None:
    t = NavTracker(nav_now=51000, nav_open_today=50000, nav_open_week=50000, peak_nav=51000)
    t.roll_day(dt.datetime(2026, 5, 12, 0, 0))  # Tuesday
    assert t.nav_open_today == 51000
    assert t.nav_open_week == 50000


def test_nav_tracker_roll_day_monday_resets_week() -> None:
    t = NavTracker(nav_now=51000, nav_open_today=50000, nav_open_week=50000, peak_nav=51000)
    t.roll_day(dt.datetime(2026, 5, 11, 0, 0))  # Monday
    assert t.nav_open_today == 51000
    assert t.nav_open_week == 51000
