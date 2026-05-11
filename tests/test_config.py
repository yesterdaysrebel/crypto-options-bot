"""Tests for the config loader. AC: loads .env + 3 strategy yamls; risk weights sum to 1.0;
disabled strategies are skipped at runtime."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from bot.config import (
    DirectionalConfig,
    IronCondorConfig,
    Settings,
    VolStrangleConfig,
    load_all,
    load_global_config,
    load_strategy_configs,
)
from bot.config.settings import BotMode, LogLevel
from bot.config.loader import ConfigError

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config"


def test_repo_global_yaml_loads() -> None:
    g = load_global_config(REPO_CONFIG)
    assert g.nav_inr == 50000.0
    assert g.usd_inr_rate == 85.0
    assert g.risk_caps.daily_loss_pct == 0.03
    assert g.risk_caps.weekly_loss_pct == 0.06
    assert g.risk_caps.lifetime_dd_pct == 0.15
    assert g.concurrency.max_total == 3
    assert g.concurrency.max_per_strategy == 1
    assert g.execution.spread_filter_max_pct == 0.08


def test_repo_strategy_yamls_load_with_correct_types() -> None:
    strategies = load_strategy_configs(REPO_CONFIG)
    by_id = {s.id.value: s for s in strategies}
    assert isinstance(by_id["directional"], DirectionalConfig)
    assert isinstance(by_id["iron_condor"], IronCondorConfig)
    assert isinstance(by_id["vol_strangle"], VolStrangleConfig)


def test_repo_risk_weights_sum_to_one() -> None:
    strategies = load_strategy_configs(REPO_CONFIG)
    total = sum(s.risk_weight for s in strategies)
    assert abs(total - 1.0) < 1e-9


def test_disabled_strategy_skipped(tmp_path: Path) -> None:
    _scaffold_configs(tmp_path, disable=("vol_strangle",))
    app = load_all(config_dir=tmp_path, env_file=None)
    enabled = [s.id.value for s in app.enabled_strategies]
    assert "vol_strangle" not in enabled
    assert {"directional", "iron_condor"}.issubset(enabled)


def test_risk_weight_sum_violation_raises(tmp_path: Path) -> None:
    _scaffold_configs(tmp_path, weights={"directional": 0.7, "iron_condor": 0.25, "vol_strangle": 0.15})
    with pytest.raises(ConfigError, match=r"risk_weights must sum to 1\.0"):
        load_strategy_configs(tmp_path)


def test_unknown_strategy_id_raises(tmp_path: Path) -> None:
    _scaffold_configs(tmp_path)
    (tmp_path / "strategies" / "rogue.yaml").write_text(
        "id: nope\nenabled: true\nrisk_weight: 0.1\nrisk_per_trade_pct: 0.01\nmax_lots_cap: 1\n"
    )
    with pytest.raises(ConfigError, match="unknown strategy id"):
        load_strategy_configs(tmp_path)


def test_extra_field_rejected_in_global(tmp_path: Path) -> None:
    _scaffold_configs(tmp_path)
    (tmp_path / "global.yaml").write_text(
        textwrap.dedent(
            """
            nav_inr: 50000
            usd_inr_rate: 85
            extra_field: not_allowed
            """
        ).strip()
    )
    with pytest.raises(Exception):  # noqa: B017 — exact pydantic error type out of scope
        load_global_config(tmp_path)


def test_settings_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_MODE", "live")
    monkeypatch.setenv("BOT_NAV_INR", "60000")
    monkeypatch.setenv("DELTA_API_KEY", "test-key")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.is_live
    assert s.nav_inr_override == 60000.0
    assert s.delta_api_key == "test-key"


def test_settings_accepts_bootstrap_env_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """VPS placeholder .env uses LOG_LEVEL; compose may set MODE (not BOT_MODE / BOT_LOG_LEVEL)."""
    monkeypatch.delenv("BOT_MODE", raising=False)
    monkeypatch.delenv("BOT_LOG_LEVEL", raising=False)
    monkeypatch.setenv("MODE", "dry")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.mode == BotMode.DRY
    assert s.log_level == LogLevel.DEBUG


def test_load_all_overrides_nav_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _scaffold_configs(tmp_path)
    monkeypatch.setenv("BOT_NAV_INR", "100000")
    app = load_all(config_dir=tmp_path, env_file=None)
    assert app.effective_nav_inr == 100000.0
    assert app.global_config.nav_inr == 50000.0


def test_strategy_by_id_round_trip() -> None:
    app = load_all(config_dir=REPO_CONFIG, env_file=None)
    d = app.strategy_by_id("directional")
    assert isinstance(d, DirectionalConfig)
    with pytest.raises(KeyError):
        app.strategy_by_id("does_not_exist")


def _scaffold_configs(
    root: Path,
    weights: dict[str, float] | None = None,
    disable: tuple[str, ...] = (),
) -> None:
    """Create a minimal valid set of yamls in `root` so loaders can exercise them."""
    weights = weights or {"directional": 0.60, "iron_condor": 0.25, "vol_strangle": 0.15}
    (root / "strategies").mkdir(parents=True, exist_ok=True)
    (root / "global.yaml").write_text(
        textwrap.dedent(
            """
            nav_inr: 50000
            usd_inr_rate: 85.0
            risk_caps:
              daily_loss_pct: 0.03
              weekly_loss_pct: 0.06
              lifetime_dd_pct: 0.15
            trading_window:
              start_ist: "09:00"
              end_ist: "22:00"
              expiry_force_close_ist: "16:45"
            concurrency:
              max_total: 3
              max_per_strategy: 1
            execution:
              spread_filter_max_pct: 0.08
              maker_limit_timeout_seconds: 30
              slip_bps_directional: 50
              slip_bps_strangle: 50
              slip_bps_condor: 100
              trail_update_throttle_seconds: 5.0
            """
        ).strip()
    )
    bodies = {
        "directional": textwrap.dedent(
            """
            id: directional
            enabled: {enabled}
            risk_weight: {w}
            risk_per_trade_pct: 0.01
            max_lots_cap: 10
            underlyings: [BTC, ETH]
            """
        ).strip(),
        "iron_condor": textwrap.dedent(
            """
            id: iron_condor
            enabled: {enabled}
            risk_weight: {w}
            risk_per_trade_pct: 0.015
            max_lots_cap: 3
            underlyings: [BTC, ETH]
            """
        ).strip(),
        "vol_strangle": textwrap.dedent(
            """
            id: vol_strangle
            enabled: {enabled}
            risk_weight: {w}
            risk_per_trade_pct: 0.01
            max_lots_cap: 5
            underlyings: [BTC, ETH]
            """
        ).strip(),
    }
    for sid, body in bodies.items():
        (root / "strategies" / f"{sid}.yaml").write_text(
            body.format(enabled=str(sid not in disable).lower(), w=weights[sid]) + "\n"
        )
