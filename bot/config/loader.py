"""YAML config loader. Bundles env Settings + GlobalConfig + per-strategy configs."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from bot.config.models import (
    DirectionalConfig,
    GlobalConfig,
    IronCondorConfig,
    StrategyConfig,
    StrategyId,
    VolStrangleConfig,
)
from bot.config.settings import Settings, load_settings

_STRATEGY_CLASS_BY_ID: dict[str, type[StrategyConfig]] = {
    StrategyId.DIRECTIONAL.value: DirectionalConfig,
    StrategyId.IRON_CONDOR.value: IronCondorConfig,
    StrategyId.VOL_STRANGLE.value: VolStrangleConfig,
}


class ConfigError(ValueError):
    """Raised for any config-loading or validation failure."""


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"config file {path} must be a YAML mapping, got {type(data).__name__}")
    return data


def load_global_config(config_dir: Path | str) -> GlobalConfig:
    path = Path(config_dir) / "global.yaml"
    raw = _read_yaml(path)
    return GlobalConfig.model_validate(raw)


def load_strategy_configs(config_dir: Path | str) -> list[StrategyConfig]:
    """Load all `config/strategies/*.yaml`, validate types, enforce that risk_weights sum to 1.0."""
    dir_path = Path(config_dir) / "strategies"
    if not dir_path.is_dir():
        raise ConfigError(f"strategy config dir not found: {dir_path}")

    configs: list[StrategyConfig] = []
    for path in sorted(dir_path.glob("*.yaml")):
        raw = _read_yaml(path)
        strategy_id = raw.get("id")
        if strategy_id is None:
            raise ConfigError(f"{path}: missing required field 'id'")
        klass = _STRATEGY_CLASS_BY_ID.get(strategy_id)
        if klass is None:
            raise ConfigError(
                f"{path}: unknown strategy id '{strategy_id}'; "
                f"expected one of {sorted(_STRATEGY_CLASS_BY_ID)}"
            )
        configs.append(klass.model_validate(raw))

    if not configs:
        raise ConfigError(f"no strategy configs found under {dir_path}")

    _check_unique_ids(configs)
    _check_risk_weights_sum_to_one(configs)
    return configs


def _check_unique_ids(configs: list[StrategyConfig]) -> None:
    seen: set[str] = set()
    for c in configs:
        if c.id.value in seen:
            raise ConfigError(f"duplicate strategy id '{c.id.value}'")
        seen.add(c.id.value)


def _check_risk_weights_sum_to_one(configs: list[StrategyConfig]) -> None:
    total = sum(c.risk_weight for c in configs)
    if not math.isclose(total, 1.0, rel_tol=0, abs_tol=1e-6):
        raise ConfigError(
            f"strategy risk_weights must sum to 1.0, got {total:.6f}; "
            f"weights = {{ {', '.join(f'{c.id.value}={c.risk_weight}' for c in configs)} }}"
        )


@dataclass(frozen=True)
class AppConfig:
    """Bundle of every config layer the app needs at startup."""

    settings: Settings
    global_config: GlobalConfig
    strategies: list[StrategyConfig]

    @property
    def enabled_strategies(self) -> list[StrategyConfig]:
        return [s for s in self.strategies if s.enabled]

    def strategy_by_id(self, sid: str | StrategyId) -> StrategyConfig:
        key = sid.value if isinstance(sid, StrategyId) else sid
        for s in self.strategies:
            if s.id.value == key:
                return s
        raise KeyError(f"strategy '{key}' not configured")

    @property
    def effective_nav_inr(self) -> float:
        """Settings env override wins over global.yaml. Useful for tests + dry-run experiments."""
        if self.settings.nav_inr_override is not None:
            return float(self.settings.nav_inr_override)
        return self.global_config.nav_inr

    @property
    def effective_usd_inr_rate(self) -> float:
        if self.settings.usd_inr_rate_override is not None:
            return float(self.settings.usd_inr_rate_override)
        return self.global_config.usd_inr_rate


def load_all(
    config_dir: Path | str | None = None,
    env_file: str | Path | None = ".env",
) -> AppConfig:
    """Load Settings + GlobalConfig + all StrategyConfigs in one call."""
    settings = load_settings(env_file)
    effective_config_dir = Path(config_dir) if config_dir is not None else settings.config_dir
    global_config = load_global_config(effective_config_dir)
    strategies = load_strategy_configs(effective_config_dir)

    logger.debug(
        "loaded config: mode={} strategies={} enabled={}",
        settings.mode.value,
        [s.id.value for s in strategies],
        [s.id.value for s in strategies if s.enabled],
    )

    return AppConfig(settings=settings, global_config=global_config, strategies=strategies)
