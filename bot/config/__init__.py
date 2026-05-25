"""Typed configuration: environment via pydantic-settings + YAML configs via pydantic models.

Public API:
    load_settings()      -> Settings
    load_global_config() -> GlobalConfig
    load_strategy_configs() -> list[StrategyConfig]
    load_all()           -> AppConfig    (everything bundled)
"""

from bot.config.loader import AppConfig, load_all, load_global_config, load_strategy_configs
from bot.config.models import (
    BaseStrategyConfig,
    ConcurrencyConfig,
    CreditVerticalConfig,
    DirectionalConfig,
    ExecutionConfig,
    GlobalConfig,
    LongStraddleConfig,
    RiskCapsConfig,
    StrategyConfig,
    StrategyId,
    TradingWindowConfig,
    Underlying,
)
from bot.config.settings import Settings, load_settings

__all__ = [
    "AppConfig",
    "BaseStrategyConfig",
    "ConcurrencyConfig",
    "CreditVerticalConfig",
    "DirectionalConfig",
    "ExecutionConfig",
    "GlobalConfig",
    "LongStraddleConfig",
    "RiskCapsConfig",
    "Settings",
    "StrategyConfig",
    "StrategyId",
    "TradingWindowConfig",
    "Underlying",
    "load_all",
    "load_global_config",
    "load_settings",
    "load_strategy_configs",
]
