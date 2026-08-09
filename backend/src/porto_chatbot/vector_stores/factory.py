"""Compatibility factory for vector repository lifecycle.

The factory resolves the **active** vector profile and returns the matching
backend (Chroma or Milvus).  In Plan 2 the active profile is always Chroma;
Plan 3 introduces activation of Milvus profiles.
"""
from __future__ import annotations

import threading

from ..settings import Settings
from .base import VectorRepository
from .chroma import ChromaVectorRepository
from .milvus import MilvusVectorRepository

_lock = threading.Lock()
_cache: dict[str, VectorRepository] = {}


class VectorRepositoryFactory:
    """Process-wide cache of VectorRepository instances keyed by profile."""

    @staticmethod
    def get(settings: Settings) -> VectorRepository:
        active = None
        try:
            from ..vector_profiles import VectorProfileStore

            profile_store = VectorProfileStore(settings)
            active = profile_store.bootstrap() or profile_store.active()
        except Exception:
            pass  # Fall back to Chroma default

        if active and active.backend == "milvus":
            key = f"milvus:{active.uri}:{active.id}"
            with _lock:
                repo = _cache.get(key)
                if repo is None:
                    repo = MilvusVectorRepository.from_profile(active)
                    _cache[key] = repo
                return repo

        # Default / Chroma
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
