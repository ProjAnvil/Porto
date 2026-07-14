"""Backwards-compatible shim.

Historically the entire FastAPI application lived in this module. It has been
split into the :mod:`porto_chatbot.api` package, but tests and other callers
still ``from porto_chatbot import main`` and access ``main.app`` /
``main.settings``. This shim re-exports those names so existing imports keep
working.

Note: ``settings`` is re-exported here (rather than read indirectly) so that
``monkeypatch.setattr(main, "settings", ...)`` in tests can override it, and
``porto_chatbot.api.deps.current_settings`` resolves the live value through
this module at call time.
"""

from __future__ import annotations

from porto_chatbot.api.app import app
from porto_chatbot.api.routes.settings import get_app_settings
from porto_chatbot.settings import settings

__all__ = ["app", "get_app_settings", "settings"]
