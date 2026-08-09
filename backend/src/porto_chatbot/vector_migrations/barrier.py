"""Process-level finalization barrier for vector migration.

When the barrier is active, new vector writes (Memory indexing) and KB
rebuild submissions are blocked, while SQLite message and intent
persistence continues normally.  This lets the coordinator perform a
final catch-up and atomic activation without competing writers.

The barrier is a best-effort cooperative mechanism within a single
process.  It is **not** a distributed lock — the SQLite-backed
``VECTOR_MIGRATION_LOCK`` in ``service_locks`` provides cross-restart
state, while this event provides real-time write suppression.
"""
from __future__ import annotations

import threading

_event = threading.Event()


def is_active() -> bool:
    """Return ``True`` while the finalization barrier holds."""
    return _event.is_set()


def set_barrier() -> None:
    """Activate the barrier — blocks new vector writes and reindex."""
    _event.set()


def clear_barrier() -> None:
    """Release the barrier — resumes normal vector writes."""
    _event.clear()


class _BarrierGuard:
    """Context manager that sets/clears the barrier with guaranteed release."""

    def __init__(self, timeout: float = 30.0):
        self._timeout = timeout
        self._timer: threading.Timer | None = None

    def __enter__(self) -> _BarrierGuard:
        set_barrier()
        # Safety net: auto-release after timeout in case the holder crashes.
        self._timer = threading.Timer(self._timeout, clear_barrier)
        self._timer.daemon = True
        self._timer.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._timer is not None:
            self._timer.cancel()
        clear_barrier()
