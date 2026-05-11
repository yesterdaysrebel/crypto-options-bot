"""Exit engine: routes a PositionState through its strategy's `manage()` method,
applies chandelier-style trailing for directional, and emits ExitDirective records
for the execution router to act on.

Strategies own the IF (when to exit/trail). This engine owns the BOOKKEEPING (peak/trough
tracking, throttling trail updates so we don't spam orders, choosing the right reduce-only
stop price on the wire).
"""

from bot.exits.engine import (
    ExitDirective,
    ExitEngine,
    ExitKind,
    PositionRuntime,
)

__all__ = ["ExitDirective", "ExitEngine", "ExitKind", "PositionRuntime"]
