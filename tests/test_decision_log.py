"""Tests for the decision-log writer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bot.analytics.decision_log import DecisionLogWriter, DecisionRecord
from bot.storage import Database, Decision, DecisionKind, DecisionReason
from sqlalchemy import select


@pytest.mark.asyncio
async def test_write_persists_strategy_decisions(db: Database) -> None:
    writer = DecisionLogWriter(db)
    n = await writer.write(
        [
            {
                "strategy_id": "directional",
                "kind": "evaluate",
                "underlying": "BTC",
                "symbol": "C-BTC-100000-150526",
                "passed": False,
                "reason": "filter_failed",
                "feature_vector": {"ema_fast": 100.0, "ema_slow": 101.0},
            },
            {
                "strategy_id": "credit_vertical",
                "kind": "evaluate",
                "underlying": "BTC",
                "passed": True,
                "reason": "passed",
                "feature_vector": {"credit_inr": 320.0},
            },
        ]
    )
    assert n == 2
    async with db.session() as session:
        rows = (await session.execute(select(Decision).order_by(Decision.id))).scalars().all()
    assert {r.strategy_id for r in rows} == {"directional", "credit_vertical"}
    assert any(r.passed for r in rows)
    assert any(not r.passed for r in rows)
    fv = next(r.feature_vector for r in rows if r.strategy_id == "directional")
    assert fv is not None and fv["ema_fast"] == 100.0


@pytest.mark.asyncio
async def test_unknown_reason_falls_back_to_other(db: Database) -> None:
    writer = DecisionLogWriter(db)
    await writer.write(
        [
            {
                "strategy_id": "directional",
                "kind": "totally_unknown",
                "passed": False,
                "reason": "not_a_real_reason",
                "feature_vector": {"x": 1},
            }
        ]
    )
    async with db.session() as session:
        rows = (await session.execute(select(Decision))).scalars().all()
    assert rows[0].kind == DecisionKind.EVALUATE.value
    assert rows[0].reason == DecisionReason.OTHER.value


@pytest.mark.asyncio
async def test_writer_mirrors_to_jsonl(db: Database, tmp_path: Path) -> None:
    mirror = tmp_path / "decisions" / "today.jsonl"
    writer = DecisionLogWriter(db, mirror_path=mirror)
    await writer.write(
        [
            {
                "strategy_id": "long_straddle",
                "kind": "evaluate",
                "passed": True,
                "reason": "passed",
                "feature_vector": {"atr_pct": 0.85},
            }
        ]
    )
    text = mirror.read_text(encoding="utf-8").strip().splitlines()
    assert len(text) == 1
    parsed = json.loads(text[0])
    assert parsed["strategy_id"] == "long_straddle"
    assert parsed["feature_vector"]["atr_pct"] == 0.85


@pytest.mark.asyncio
async def test_write_batches_in_chunks(db: Database) -> None:
    writer = DecisionLogWriter(db, max_batch=3)
    payload = [
        {
            "strategy_id": "directional",
            "kind": "evaluate",
            "passed": False,
            "reason": "passed",
            "feature_vector": {"i": i},
        }
        for i in range(10)
    ]
    n = await writer.write(payload)
    assert n == 10
    async with db.session() as session:
        count = (await session.execute(select(Decision))).scalars().all()
    assert len(count) == 10


def test_record_from_dict_accepts_known_strategy_decisions() -> None:
    rec = DecisionRecord.from_dict(
        {"strategy_id": "directional", "kind": "evaluate", "reason": "spread_too_wide", "passed": False}
    )
    assert rec.strategy_id == "directional"
    assert rec.reason == DecisionReason.SPREAD_TOO_WIDE.value
