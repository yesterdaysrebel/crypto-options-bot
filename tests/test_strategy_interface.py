"""Tests for the strategy interface, registry, and dispatcher.

AC: 1) registry rejects duplicate IDs; 2) risk budget = NAV * weight * per_trade_pct;
3) dispatcher isolates strategy errors; 4) only enabled strategies are dispatched.
"""

from __future__ import annotations

import datetime as dt

import pytest
from bot.config.models import (
    DirectionalConfig,
    IronCondorConfig,
    StrategyId,
    Underlying,
    VolStrangleConfig,
)
from bot.strategies import (
    Action,
    ActionType,
    Intent,
    MarketState,
    PositionState,
    Strategy,
    StrategyDispatcher,
    StrategyRegistry,
)


class _Stub(Strategy):
    """Minimal Strategy double for tests."""

    def __init__(self, config, intents=(), decisions=(), raise_on_evaluate=False, actions=()):
        super().__init__(config)
        self.id = StrategyId(config.id)
        self._intents = list(intents)
        self._decisions = list(decisions)
        self._actions = list(actions)
        self._raise_on_evaluate = raise_on_evaluate

    def evaluate(self, market):
        if self._raise_on_evaluate:
            raise RuntimeError("boom")
        return self._intents, self._decisions

    def manage(self, position, market):
        return self._actions


def _now() -> dt.datetime:
    return dt.datetime(2026, 5, 12, 10, 0, 0)


def _market() -> MarketState:
    return MarketState(now=_now(), chain=None, candles_by_tf={}, underlying_marks={})  # type: ignore[arg-type]


def _directional_config(enabled=True, weight=0.60, per_trade=0.01) -> DirectionalConfig:
    return DirectionalConfig.model_validate(
        {
            "id": "directional",
            "enabled": enabled,
            "risk_weight": weight,
            "risk_per_trade_pct": per_trade,
            "max_lots_cap": 10,
        }
    )


def _condor_config(enabled=True, weight=0.25, per_trade=0.015) -> IronCondorConfig:
    return IronCondorConfig.model_validate(
        {
            "id": "iron_condor",
            "enabled": enabled,
            "risk_weight": weight,
            "risk_per_trade_pct": per_trade,
            "max_lots_cap": 3,
        }
    )


def _strangle_config(enabled=True, weight=0.15, per_trade=0.01) -> VolStrangleConfig:
    return VolStrangleConfig.model_validate(
        {
            "id": "vol_strangle",
            "enabled": enabled,
            "risk_weight": weight,
            "risk_per_trade_pct": per_trade,
            "max_lots_cap": 5,
        }
    )


def test_registry_rejects_duplicate_ids() -> None:
    a = _Stub(_directional_config())
    b = _Stub(_directional_config())
    with pytest.raises(ValueError, match="duplicate strategy"):
        StrategyRegistry([a, b])


def test_registry_get_and_contains() -> None:
    a = _Stub(_directional_config())
    b = _Stub(_condor_config())
    reg = StrategyRegistry([a, b])
    assert len(reg) == 2
    assert StrategyId.DIRECTIONAL in reg
    assert "iron_condor" in reg
    assert reg.get("directional") is a
    assert reg.get(StrategyId.IRON_CONDOR) is b


def test_registry_enabled_filters_disabled() -> None:
    a = _Stub(_directional_config(enabled=True))
    b = _Stub(_condor_config(enabled=False))
    reg = StrategyRegistry([a, b])
    assert reg.enabled() == [a]


def test_risk_budget_inr_computation() -> None:
    reg = StrategyRegistry(
        [
            _Stub(_directional_config(weight=0.6, per_trade=0.01)),
            _Stub(_condor_config(weight=0.25, per_trade=0.015)),
            _Stub(_strangle_config(weight=0.15, per_trade=0.01)),
        ]
    )
    nav = 50_000.0
    assert reg.risk_budget_inr(nav, "directional") == pytest.approx(50_000 * 0.6 * 0.01)
    assert reg.risk_budget_inr(nav, "iron_condor") == pytest.approx(50_000 * 0.25 * 0.015)
    assert reg.risk_budget_inr(nav, "vol_strangle") == pytest.approx(50_000 * 0.15 * 0.01)


def test_dispatcher_collects_intents_and_decisions() -> None:
    intent = Intent(
        strategy_id=StrategyId.DIRECTIONAL,
        underlying=Underlying.BTC,
        bucket="D1",  # type: ignore[arg-type]
        legs=[],
        requested_lots=5,
        rationale="test",
    )
    a = _Stub(
        _directional_config(), intents=[intent], decisions=[{"strategy_id": "directional", "passed": True}]
    )
    b = _Stub(_condor_config(), intents=[], decisions=[{"strategy_id": "iron_condor", "passed": False}])
    reg = StrategyRegistry([a, b])
    disp = StrategyDispatcher(reg)
    result = disp.evaluate_all(_market())
    assert len(result.all_intents) == 1
    assert len(result.all_decisions) == 2
    assert StrategyId.DIRECTIONAL in result.eval_time_ms


def test_dispatcher_isolates_strategy_errors() -> None:
    bad = _Stub(_directional_config(), raise_on_evaluate=True)
    good = _Stub(_condor_config(), decisions=[{"strategy_id": "iron_condor", "passed": True}])
    reg = StrategyRegistry([bad, good])
    disp = StrategyDispatcher(reg)
    result = disp.evaluate_all(_market())
    assert StrategyId.DIRECTIONAL in result.errors
    assert "boom" in result.errors[StrategyId.DIRECTIONAL]
    assert StrategyId.IRON_CONDOR in result.decisions_by_strategy
    assert StrategyId.IRON_CONDOR not in result.errors


def test_dispatcher_manage_routes_to_owner() -> None:
    action = Action(kind=ActionType.NO_OP)
    s = _Stub(_directional_config(), actions=[action])
    reg = StrategyRegistry([s])
    disp = StrategyDispatcher(reg)
    pos = PositionState(
        trade_id=1,
        strategy_id=StrategyId.DIRECTIONAL,
        underlying=Underlying.BTC,
        expiry=_now() + dt.timedelta(days=1),
        lots=5,
        entry_ts=_now(),
    )
    res = disp.manage_all([pos], _market())
    assert res.actions_by_position[1] == [action]


def test_dispatcher_skips_when_only_filter_excludes() -> None:
    a = _Stub(_directional_config(), decisions=[{"strategy_id": "directional"}])
    b = _Stub(_condor_config(), decisions=[{"strategy_id": "iron_condor"}])
    reg = StrategyRegistry([a, b])
    disp = StrategyDispatcher(reg)
    res = disp.evaluate_all(_market(), only={StrategyId.DIRECTIONAL})
    assert set(res.decisions_by_strategy.keys()) == {StrategyId.DIRECTIONAL}
