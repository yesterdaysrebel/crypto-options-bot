"""Prefetch IV percentiles for strategy evaluation on the main tick loop."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping

from bot.config.models import DirectionalConfig, ExpiryBucket, StrategyConfig, StrategyId, Underlying
from bot.data.chain_cache import ChainCache
from bot.desk.iv_history import IvHistoryStore, IvPercentileResult, atm_iv_for_bucket


def directional_needs_iv_prefetch(cfg: DirectionalConfig) -> bool:
    desk = cfg.desk
    return desk.max_iv_percentile_long is not None or desk.min_iv_percentile_long is not None


async def prefetch_directional_iv_percentiles(
    iv_history: IvHistoryStore,
    chain: ChainCache,
    underlying_marks: Mapping[Underlying, float],
    dir_cfg: DirectionalConfig,
    *,
    now: dt.datetime,
) -> dict[tuple[Underlying, ExpiryBucket], IvPercentileResult]:
    out: dict[tuple[Underlying, ExpiryBucket], IvPercentileResult] = {}
    buckets = {dir_cfg.expiry.prefer, dir_cfg.expiry.fallback}
    for underlying in dir_cfg.underlyings:
        spot = underlying_marks.get(underlying)
        if spot is None:
            continue
        for bucket in buckets:
            atm_iv = atm_iv_for_bucket(chain, underlying, bucket, float(spot), now)
            out[(underlying, bucket)] = await iv_history.iv_percentile(
                underlying,
                bucket,
                atm_iv,
                now=now,
            )
    return out


async def prefetch_iv_for_strategies(
    iv_history: IvHistoryStore,
    chain: ChainCache,
    underlying_marks: Mapping[Underlying, float],
    strategy_configs: Mapping[StrategyId, StrategyConfig],
    *,
    now: dt.datetime,
) -> dict[tuple[Underlying, ExpiryBucket], IvPercentileResult]:
    merged: dict[tuple[Underlying, ExpiryBucket], IvPercentileResult] = {}
    dir_cfg = strategy_configs.get(StrategyId.DIRECTIONAL)
    if isinstance(dir_cfg, DirectionalConfig) and directional_needs_iv_prefetch(dir_cfg):
        merged.update(
            await prefetch_directional_iv_percentiles(
                iv_history,
                chain,
                underlying_marks,
                dir_cfg,
                now=now,
            )
        )
    return merged
