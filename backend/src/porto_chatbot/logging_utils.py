"""Centralized logging configuration powered by loguru.

Architecture
------------
Existing code continues to use ``get_component_logger()``, which returns a
stdlib :class:`logging.Logger`.  Records propagate to the root logger where
an :class:`InterceptHandler` forwards them to loguru's sinks::

    +--------------------------------------+
    | Existing code (stdlib logging API)   |
    | logger = get_component_logger("api") |
    | logger.info("msg %s", val)           |   %-style works natively
    +-----------------+--------------------+
                      |  propagate = True
                      v
    +--------------------------------------+
    | Root logger -> InterceptHandler      |
    |   record.getMessage()                |   %-style formatted here
    +-----------------+--------------------+
                      |
                      v
    +--------------------------------------+
    | loguru.logger (singleton)            |
    |   sink: {log_dir}/app.log            |   enqueue=True, rotation,
    |   sink: stderr                       |   retention, compression
    +--------------------------------------+

New code may use :func:`get_logger` for a native loguru logger::

    from porto_chatbot.logging_utils import get_logger
    log = get_logger("new-module")
    log.info("msg {}", val)         # loguru {}-style

Both APIs share the same sinks, rotation policy, and enqueue safety.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger as _loguru

if TYPE_CHECKING:
    from porto_chatbot.settings import Settings

__all__ = [
    "setup_logging",
    "get_logger",
    "get_component_logger",
    "InterceptHandler",
    "reset_logging",
]

# ---------------------------------------------------------------------------
# Format strings
# ---------------------------------------------------------------------------

# Console (stderr) -- colourised, for local dev / docker logs
_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> "
    "| <level>{level: <8}</level> "
    "| <cyan>{extra[component]}</cyan> "
    "| <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> "
    "- <level>{message}</level>"
)

# File -- plain text, no colour codes, greppable
_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} "
    "| {level: <8} "
    "| {extra[component]} "
    "| {name}:{function}:{line} "
    "- {message}"
)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_ROTATION = "10 MB"
_DEFAULT_RETENTION = "10 days"
_DEFAULT_COMPRESSION = "zip"
_DEFAULT_LEVEL = "INFO"

# ---------------------------------------------------------------------------
# Internal state (guarded by _setup_lock)
# ---------------------------------------------------------------------------

_configured_log_dir: Path | None = None
_setup_lock = threading.Lock()


# ---------------------------------------------------------------------------
# InterceptHandler -- redirect stdlib logging -> loguru
# ---------------------------------------------------------------------------


class InterceptHandler(logging.Handler):
    """Forward stdlib :mod:`logging` records to loguru.

    Installed on the **root** logger so that every third-party library
    (LangChain, Chroma, OpenAI, uvicorn, ...) is captured by the same
    loguru sinks configured in :func:`setup_logging`.
    """

    def emit(self, record: logging.LogRecord) -> None:
        # Map the stdlib level name to a loguru level.
        try:
            level: str | int = _loguru.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Walk the call stack past logging-internal frames so that loguru
        # reports the *real* source location (file/function/line).
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        # Derive component from the stdlib logger name.
        name = record.name
        if name.startswith("porto_chatbot."):
            component = name[len("porto_chatbot.") :]
        else:
            component = name

        _loguru.bind(component=component).opt(
            depth=depth, exception=record.exc_info
        ).log(level, record.getMessage())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def setup_logging(
    settings: Settings | None = None,
    *,
    level: str = _DEFAULT_LEVEL,
    enqueue: bool = True,
    diagnose: bool = False,
    rotation: str = _DEFAULT_ROTATION,
    retention: str = _DEFAULT_RETENTION,
    compression: str | None = _DEFAULT_COMPRESSION,
) -> None:
    """Configure (or re-configure) the global loguru logger.

    Safe to call multiple times -- each invocation removes all existing
    sinks and installs fresh ones.

    Parameters
    ----------
    settings:
        Used to resolve ``log_dir``. Falls back to the global
        :data:`porto_chatbot.settings.settings`.
    level:
        Minimum severity for all sinks.
    enqueue:
        When *True* (default) every sink receives records through an
        internal queue processed by a background thread.  This makes
        logging **non-blocking, thread-safe, and multiprocess-safe**.
    diagnose:
        When *True*, loguru expands variable values in exception
        tracebacks.  **Disable in production** to avoid leaking
        sensitive data.
    rotation:
        Size or time threshold for log file rotation (default ``"10 MB"``).
    retention:
        How long to keep rotated logs (default ``"10 days"``).
    compression:
        Compression format for rotated logs (default ``"zip"``).
    """
    global _configured_log_dir

    from porto_chatbot.settings import settings as _fallback

    resolved = settings or _fallback
    log_dir: Path = resolved.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    # --- Remove all existing sinks (idempotent re-configuration) ---------
    _loguru.remove()

    # Set default component for records without an explicit bind.
    _loguru.configure(extra={"component": "app"})

    # --- 1. Unified rotating file sink -----------------------------------
    file_sink_kwargs: dict = {
        "level": level,
        "format": _FILE_FORMAT,
        "rotation": rotation,
        "retention": retention,
        "enqueue": enqueue,
        "encoding": "utf-8",
        "backtrace": True,
        "diagnose": diagnose,
        "catch": True,
    }
    if compression:
        file_sink_kwargs["compression"] = compression
    _loguru.add(log_dir / "app.log", **file_sink_kwargs)

    # --- 2. Stderr sink (container / terminal visibility) ----------------
    _loguru.add(
        sys.stderr,
        level=level,
        format=_CONSOLE_FORMAT,
        enqueue=enqueue,
        backtrace=True,
        diagnose=diagnose,
        catch=True,
    )

    # --- 3. Intercept stdlib logging -------------------------------------
    #   force=True clears existing root handlers (e.g. uvicorn defaults)
    #   so that everything flows through loguru.
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

    # Also intercept uvicorn's dedicated loggers (they set propagate=False).
    for _name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        _lg = logging.getLogger(_name)
        _lg.handlers = [InterceptHandler()]
        _lg.propagate = False

    _configured_log_dir = log_dir.resolve()


def reset_logging() -> None:
    """Reset all logging state.

    Used by the test fixture to ensure each test starts with a clean
    logger configuration.
    """
    global _configured_log_dir
    _configured_log_dir = None
    _loguru.remove()


def _ensure_configured(settings: Settings | None = None) -> None:
    """Lazily initialise logging on first use or when log_dir changes."""
    global _configured_log_dir

    from porto_chatbot.settings import settings as _fallback

    resolved = settings or _fallback
    target = resolved.log_dir.resolve()

    if _configured_log_dir != target:
        with _setup_lock:
            # Double-check after acquiring the lock.
            if _configured_log_dir != target:
                setup_logging(resolved)


def get_logger(component: str = "app", settings: Settings | None = None):
    """Return a **loguru** logger bound to *component*.

    Use this in new code::

        from porto_chatbot.logging_utils import get_logger
        logger = get_logger("my_module")
        logger.info("processed {} items in {:.1f}s", count, elapsed)

    Returns
    -------
    loguru.Logger
        A bound logger with ``component`` set in ``extra``.
    """
    _ensure_configured(settings)
    return _loguru.bind(component=_sanitize_component(component))


def get_component_logger(
    component: str = "app",
    settings: Settings | None = None,
) -> logging.Logger:
    """Return a **stdlib** :class:`logging.Logger` for *component*.

    Drop-in replacement for the legacy factory.  All existing call sites
    work unchanged -- including ``%-style`` positional formatting::

        logger = get_component_logger("api")
        logger.info("request %s %s", method, path)

    The returned logger has ``propagate = True`` and **no file handler**;
    records flow to the root logger's :class:`InterceptHandler` and from
    there into loguru's sinks (file + stderr, with enqueue and rotation).
    """
    _ensure_configured(settings)
    safe = _sanitize_component(component)
    py_logger = logging.getLogger(f"porto_chatbot.{safe}")
    py_logger.setLevel(logging.INFO)
    py_logger.propagate = True  # reach root -> InterceptHandler -> loguru
    return py_logger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitize_component(component: str) -> str:
    """Normalise a component name for safe use in log metadata."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", component).strip("._")
    return cleaned or "app"
