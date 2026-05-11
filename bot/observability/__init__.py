"""Observability subsystem.

* `MetricsRegistry`: Prometheus counters / gauges for trades, decisions, NAV, etc.
* `MetricsServer`: async HTTP server exposing `/metrics` (Prometheus scrape) and `/health`.
* `TextfileCollector`: dumps current metric values to a textfile every N seconds, so a
  node_exporter --collector.textfile sidecar can scrape them even if the bot's HTTP port
  is unreachable (e.g. during reconcile or boot).
* `render_status_dashboard`: one-screen `rich` table used by `bot status`.
"""

from bot.observability.metrics import MetricsRegistry, TextfileCollector
from bot.observability.server import MetricsServer
from bot.observability.status import render_status_dashboard

__all__ = [
    "MetricsRegistry",
    "MetricsServer",
    "TextfileCollector",
    "render_status_dashboard",
]
