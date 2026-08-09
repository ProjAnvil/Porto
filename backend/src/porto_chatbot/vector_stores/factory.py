"""Compatibility factory for vector repository lifecycle.

Before profiles exist, repository clients are cached by ``data_dir`` only.
All runtime construction must go through this factory so that shutdown can
clean up all open clients in one place.
"""
from __future__ import annotations

import threading

from ..settings import Settings
from .base import VectorRepository
from .chroma import ChromaVectorRepository

_lock = threading.Lock()
_cache: dict[str, VectorRepository] = {}


class VectorRepositoryFactory:
    """Process-wide cache of VectorRepository instances keyed by data_dir."""

    @staticmethod
    def get(settings: Settings) -> VectorRepository:
        key = str(settings.chroma_dir)
        with _lock:
            repo = _cache.get(key)
            if repo is None:
                repo = ChromaVectorRepository(settings.chroma_dir)
                _cache[key] = repo
            return repo

    @staticmethod
    def close_all() -> None:
        """Close and evict every cached repository (call on app shutdown)."""
        with _lock:
            for repo in _cache.values():
                repo.close()
            _cache.clear()

    @staticmethod
    def clear() -> None:
        """Test-only: drop the cache without closing clients."""
        with _lock:
            _cache.clear()
