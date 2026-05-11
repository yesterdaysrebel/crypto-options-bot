"""Execution router: turns sized intents + exit directives into orders on Delta India.

Two backends share a common ExecutionRouter interface:
    LiveExecutor    - real Delta REST calls
    DryExecutor     - in-process simulator with mid+slippage fills
"""

from bot.execution.client_id import generate_client_order_id
from bot.execution.dry import DryExecutor
from bot.execution.live import LiveExecutor
from bot.execution.reconcile import (
    OrderReconciler,
    ReconcileError,
    ReconcileMismatch,
    ReconcileReport,
)
from bot.execution.router import (
    EntryRequest,
    EntryResult,
    ExecutionRouter,
    ExitRequest,
    ExitResult,
    LegFill,
    OrderTicket,
)

__all__ = [
    "DryExecutor",
    "EntryRequest",
    "EntryResult",
    "ExecutionRouter",
    "ExitRequest",
    "ExitResult",
    "LegFill",
    "LiveExecutor",
    "OrderReconciler",
    "OrderTicket",
    "ReconcileError",
    "ReconcileMismatch",
    "ReconcileReport",
    "generate_client_order_id",
]
