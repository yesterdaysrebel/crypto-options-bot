"""Prometheus metrics registry + textfile collector.

Why a textfile collector when we already expose /metrics?
   * /metrics on the bot HTTP port is only reachable while the bot is up.
   * If the bot is unhealthy or restarting, Prometheus shows stale "no data".
   * Node exporter's textfile collector can read `*.prom` files (set `PROM_TEXTFILE_PATH`
     to that dir on hosts where the bot user may write there; Docker default is under
     `./runtime/metrics/` which matches the prod volume mount).
     This means even a crashed bot leaves *some* metrics behind, dating its last write.
     Combined with `bot_last_tick_seconds`, it makes "is the bot alive?" trivial.
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


class MetricsRegistry:
    """Centralised holder for all bot metrics. One instance per process."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()

        common_labels = ("strategy_id",)

        self.ticks_total = Counter("bot_ticks_total", "Number of dispatcher ticks", registry=self.registry)
        self.decisions_total = Counter(
            "bot_decisions_total",
            "Decisions written, labelled by strategy + outcome",
            ["strategy_id", "passed", "reason"],
            registry=self.registry,
        )
        self.intents_total = Counter(
            "bot_intents_total",
            "Intents emitted by strategies",
            common_labels,
            registry=self.registry,
        )
        self.trades_opened_total = Counter(
            "bot_trades_opened_total",
            "Trades successfully opened",
            common_labels,
            registry=self.registry,
        )
        self.trades_closed_total = Counter(
            "bot_trades_closed_total",
            "Trades closed, labelled by exit reason",
            ["strategy_id", "exit_reason"],
            registry=self.registry,
        )
        self.trade_pnl_inr = Histogram(
            "bot_trade_pnl_inr",
            "Realised PnL per closed trade, INR",
            common_labels,
            buckets=[-2000, -1000, -500, -200, -100, 0, 100, 200, 500, 1000, 2000, 5000],
            registry=self.registry,
        )
        self.open_positions = Gauge(
            "bot_open_positions",
            "Currently open positions",
            common_labels,
            registry=self.registry,
        )
        self.nav_inr = Gauge("bot_nav_inr", "Current NAV in INR", registry=self.registry)
        self.peak_nav_inr = Gauge("bot_peak_nav_inr", "Lifetime peak NAV in INR", registry=self.registry)
        self.drawdown_pct = Gauge(
            "bot_drawdown_pct",
            "Drawdown from peak NAV in %",
            registry=self.registry,
        )
        self.circuit_breaker_tripped = Gauge(
            "bot_circuit_breaker_tripped",
            "1 if lifetime DD circuit breaker is tripped",
            registry=self.registry,
        )
        self.exec_latency_ms = Histogram(
            "bot_exec_latency_ms",
            "End-to-end latency of submit_entry calls (ms)",
            common_labels,
            buckets=[5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000],
            registry=self.registry,
        )
        self.last_tick_seconds = Gauge(
            "bot_last_tick_seconds",
            "Unix timestamp of the last completed tick (liveness)",
            registry=self.registry,
        )
        self.ws_disconnects_total = Counter(
            "bot_ws_disconnects_total",
            "Delta WS disconnect events",
            registry=self.registry,
        )
        self.rest_errors_total = Counter(
            "bot_rest_errors_total",
            "Delta REST error responses",
            ["endpoint", "status"],
            registry=self.registry,
        )
        self.portfolio_delta_inr = Gauge(
            "bot_portfolio_delta_inr",
            "Book net delta notional in INR (abs spot-weighted)",
            registry=self.registry,
        )
        self.portfolio_vega_inr = Gauge(
            "bot_portfolio_vega_inr",
            "Book net vega notional in INR",
            registry=self.registry,
        )
        self.iv_percentile = Gauge(
            "bot_iv_percentile",
            "ATM IV percentile for underlying x expiry bucket",
            ["underlying", "expiry_bucket"],
            registry=self.registry,
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)


class TextfileCollector:
    """Periodic dump of current metrics to a `.prom` file for node_exporter pickup."""

    def __init__(self, registry: MetricsRegistry, path: Path) -> None:
        self._registry = registry
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write_once(self) -> Path:
        # Atomic write: tmp file in the same dir, then os.replace.
        data = self._registry.render()
        tmp_path: Path | None = None
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=self._path.parent,
            prefix=self._path.name + ".",
            suffix=".prom",
            delete=False,
        ) as tmp:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        assert tmp_path is not None
        os.replace(tmp_path, self._path)
        return self._path


def labelled_counter_increments(
    counter: Counter,
    labels_seq: Sequence[Sequence[str]],
) -> None:
    """Increment a labelled counter for every label tuple in `labels_seq`. Test helper."""
    for labels in labels_seq:
        counter.labels(*labels).inc()


__all__ = ["MetricsRegistry", "TextfileCollector", "labelled_counter_increments"]


def _now_seconds() -> float:
    return dt.datetime.now(dt.UTC).timestamp()
