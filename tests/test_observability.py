"""Tests for MetricsRegistry, MetricsServer, TextfileCollector, status dashboard."""

from __future__ import annotations

import asyncio
import datetime as dt
import http.client
from pathlib import Path

import pytest
from bot.observability.metrics import MetricsRegistry, TextfileCollector
from bot.observability.server import MetricsServer
from bot.observability.status import render_status_dashboard
from bot.storage import DailyPnl, Database, NavHistory, Trade, TradeStatus


def test_metrics_registry_renders_prometheus_text() -> None:
    reg = MetricsRegistry()
    reg.ticks_total.inc()
    reg.trades_opened_total.labels("directional").inc()
    reg.nav_inr.set(50_500.0)
    reg.drawdown_pct.set(2.5)
    reg.circuit_breaker_tripped.set(0)
    body = reg.render().decode("utf-8")
    assert "bot_ticks_total" in body
    assert "bot_trades_opened_total" in body
    assert 'strategy_id="directional"' in body
    assert "bot_nav_inr 50500.0" in body
    assert "bot_drawdown_pct 2.5" in body


def test_textfile_collector_writes_prom_atomically(tmp_path: Path) -> None:
    reg = MetricsRegistry()
    reg.ticks_total.inc()
    collector = TextfileCollector(reg, tmp_path / "bot.prom")
    written = collector.write_once()
    assert written.exists()
    content = written.read_text(encoding="utf-8")
    assert "bot_ticks_total" in content


@pytest.mark.asyncio
async def test_metrics_server_serves_health_and_metrics() -> None:
    reg = MetricsRegistry()
    reg.ticks_total.inc()

    async def liveness() -> bool:
        return True

    async def extra() -> dict[str, object]:
        return {"mode": "dry", "open_trades": 0}

    server = MetricsServer(reg, host="127.0.0.1", port=0, liveness_check=liveness, liveness_extra=extra)
    await server.start()
    assert server._server is not None
    sockets = server._server.sockets
    assert sockets
    port = sockets[0].getsockname()[1]

    def _get(path: str) -> tuple[int, bytes, dict[str, str]]:
        conn = http.client.HTTPConnection("127.0.0.1", port)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp.read(), dict(resp.getheaders())
        finally:
            conn.close()

    metrics_status, metrics_body, metrics_headers = await asyncio.to_thread(_get, "/metrics")
    assert metrics_status == 200
    assert metrics_headers["Content-Type"].startswith("text/plain")
    assert b"bot_ticks_total" in metrics_body

    health_status, health_body, health_headers = await asyncio.to_thread(_get, "/health")
    assert health_status == 200
    assert health_headers["Content-Type"].startswith("application/json")
    assert b'"status": "ok"' in health_body
    assert b'"mode": "dry"' in health_body

    nf_status, _, _ = await asyncio.to_thread(_get, "/nope")
    assert nf_status == 404

    await server.stop()


@pytest.mark.asyncio
async def test_metrics_server_returns_503_when_liveness_fails() -> None:
    reg = MetricsRegistry()

    async def liveness() -> bool:
        return False

    server = MetricsServer(reg, host="127.0.0.1", port=0, liveness_check=liveness)
    await server.start()
    assert server._server is not None
    port = server._server.sockets[0].getsockname()[1]

    def _get(path: str) -> int:
        conn = http.client.HTTPConnection("127.0.0.1", port)
        try:
            conn.request("GET", path)
            return conn.getresponse().status
        finally:
            conn.close()

    status = await asyncio.to_thread(_get, "/health")
    assert status == 503
    await server.stop()


@pytest.mark.asyncio
async def test_status_dashboard_renders_with_data(db: Database) -> None:
    today = dt.date.today()
    async with db.session() as session:
        session.add(
            NavHistory(
                trading_date=today,
                nav_inr=50_500.0,
                peak_nav_inr=51_000.0,
                drawdown_from_peak_pct=0.98,
                circuit_breaker_tripped=False,
            )
        )
        session.add(
            DailyPnl(
                trading_date=today,
                strategy_id="directional",
                n_trades=2,
                n_wins=1,
                n_losses=1,
                gross_pnl_inr=400.0,
                net_pnl_inr=350.0,
                fees_inr=50.0,
                win_rate=0.5,
            )
        )
        session.add(
            Trade(
                strategy_id="credit_vertical",
                underlying="BTC",
                entry_ts=dt.datetime.now(),
                status=TradeStatus.OPEN.value,
                lots=1,
            )
        )
    text = await render_status_dashboard(db)
    assert "NAV" in text
    assert "50,500" in text
    assert "directional" in text
    assert "Open Positions" in text


@pytest.mark.asyncio
async def test_status_dashboard_handles_empty_db(db: Database) -> None:
    text = await render_status_dashboard(db)
    assert "NAV" in text
    assert "no NAV history yet" in text
