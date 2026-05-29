"""Tests for the per-trade Markdown journal generator."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from bot.analytics.journal import TradeJournal
from bot.storage import (
    Database,
    Leg,
    LegStatus,
    Order,
    OrderState,
    Signal,
    Trade,
    TradeStatus,
)


async def _seed_closed_directional_trade(db: Database) -> int:
    async with db.session() as session:
        signal = Signal(
            strategy_id="directional",
            underlying="BTC",
            side="long",
            intended_symbol="C-BTC-100000-150526",
            intended_expiry=dt.datetime(2026, 5, 15, 17, 30),
            intended_strike=100_000.0,
            intended_lots=1,
            intended_premium_inr=320.0,
            feature_vector={"ema_fast": 100200.0, "ema_slow": 100100.0, "atr": 250.0},
        )
        session.add(signal)
        await session.flush()
        trade = Trade(
            strategy_id="directional",
            underlying="BTC",
            entry_ts=dt.datetime(2026, 5, 14, 14, 0),
            exit_ts=dt.datetime(2026, 5, 14, 17, 0),
            status=TradeStatus.CLOSED.value,
            mode="dry",
            lots=1,
            premium_paid_inr=320.0,
            realised_pnl_inr=480.0,
            fees_inr=20.0,
            delta_pnl_inr=400.0,
            theta_pnl_inr=80.0,
            r_multiple=1.5,
            slippage_bps=18.0,
            peak_pnl_inr=520.0,
            trough_pnl_inr=-40.0,
            entry_iv=0.62,
            exit_iv=0.58,
            exit_reason="target",
            signal_id=signal.id,
            notes={
                "trailing_stop": "broke even at +1R",
                "exit_path": "target",
                "entry_greeks": {
                    "C-BTC-100000-150526": {
                        "iv": 0.62,
                        "delta": 0.48,
                        "gamma": 0.0001,
                        "theta": -10.0,
                        "vega": 7.5,
                        "open_interest": 200.0,
                    },
                },
                "exit_greeks": {
                    "C-BTC-100000-150526": {
                        "iv": 0.58,
                        "delta": 0.52,
                        "gamma": 0.0001,
                        "theta": -9.0,
                        "vega": 7.0,
                        "open_interest": 210.0,
                    },
                },
            },
        )
        session.add(trade)
        await session.flush()
        leg = Leg(
            trade_id=trade.id,
            strategy_id="directional",
            leg_idx=0,
            symbol="C-BTC-100000-150526",
            option_type="call",
            strike=100_000.0,
            side="buy",
            lots=1,
            entry_price=320.0,
            exit_price=800.0,
            pnl_inr=480.0,
            status=LegStatus.CLOSED.value,
        )
        session.add(leg)
        order = Order(
            ts=dt.datetime(2026, 5, 14, 14, 0),
            strategy_id="directional",
            trade_id=trade.id,
            leg_idx=0,
            client_order_id="directional-1-0-entry-abc",
            symbol="C-BTC-100000-150526",
            side="buy",
            order_type="limit_post_only",
            limit_price=320.0,
            qty=1,
            filled_qty=1,
            filled_price=320.0,
            state=OrderState.FILLED.value,
        )
        session.add(order)
        await session.flush()
        return trade.id


@pytest.mark.asyncio
async def test_journal_writes_file_with_expected_sections(db: Database, tmp_path: Path) -> None:
    trade_id = await _seed_closed_directional_trade(db)
    journal = TradeJournal(db, journals_dir=tmp_path)
    path = await journal.write_for_trade(trade_id)
    assert path is not None
    assert path.parent.name == "2026-05-14"
    assert path.name == f"directional__{trade_id}.md"
    content = path.read_text(encoding="utf-8")
    assert "# Trade #" in content
    assert "## Summary" in content
    assert "## P&L" in content
    assert "## Signal (Entry Context)" in content
    assert "## Legs" in content
    assert "## Orders" in content
    assert "## Trade outcome" in content
    assert "## Wallet" in content
    assert "## Greeks" in content
    assert "C-BTC-100000-150526" in content
    assert "## Notes" in content
    assert "directional-1-0-entry-abc" in content
    assert "ema_fast" in content
    assert "trailing_stop" in content


@pytest.mark.asyncio
async def test_open_journal_includes_wallet_and_indicators(db: Database, tmp_path: Path) -> None:
    async with db.session() as session:
        sig = Signal(
            strategy_id="directional",
            underlying="BTC",
            side="long",
            intended_symbol="C-BTC-100000-150526",
            intended_expiry=dt.datetime(2026, 5, 15, 17, 30),
            intended_strike=100_000.0,
            intended_lots=1,
            intended_premium_inr=320.0,
            feature_vector={"spot": 100_000.0},
        )
        session.add(sig)
        await session.flush()
        trade = Trade(
            strategy_id="directional",
            underlying="BTC",
            entry_ts=dt.datetime(2026, 5, 14, 14, 0),
            status=TradeStatus.OPEN.value,
            mode="dry",
            lots=1,
            premium_paid_inr=320.0,
            signal_id=sig.id,
            notes={
                "wallet_at_entry": {
                    "ts": "2026-05-14T14:00:00",
                    "balances": [{"asset_symbol": "INR", "balance": 50000.0}],
                },
                "indicators_at_entry": {"spot": 100_000.0, "15m_n": 20},
                "unrealized_pnl_inr": 12.5,
                "peak_pnl_inr": 15.0,
            },
        )
        session.add(trade)
        await session.flush()
        open_tid = trade.id
    journal = TradeJournal(db, journals_dir=tmp_path)
    path = await journal.write_open_trade(open_tid)
    assert path is not None
    assert path.name.endswith("_open.md")
    text = path.read_text(encoding="utf-8")
    assert "## Wallet (at entry)" in text
    assert "INR" in text
    assert "## Indicators (at entry)" in text
    assert "15m_n" in text


@pytest.mark.asyncio
async def test_journal_returns_none_when_trade_missing(db: Database, tmp_path: Path) -> None:
    journal = TradeJournal(db, journals_dir=tmp_path)
    path = await journal.write_for_trade(9999)
    assert path is None
    assert not list(tmp_path.iterdir())
