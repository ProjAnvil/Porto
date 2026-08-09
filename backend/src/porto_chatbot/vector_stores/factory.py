"""Revision-aware factory for vector repository lifecycle.

The factory resolves the **active** vector profile and returns the matching
backend (Chroma or Milvus).  Cache keys include ``(data_dir, profile_id,
revision)`` so that a successful activation — which increments the revision
— causes stale clients to be evicted and the destination adapter to be
constructed on the next request.
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
    """Process-wide cache of VectorRepository instances keyed by profile+revision."""

    @staticmethod
    def get(settings: Settings) -> VectorRepository:
        active = None
        revision = 1
        try:
            from ..vector_profiles import VectorProfileStore

            profile_store = VectorProfileStore(settings)
            active = profile_store.bootstrap() or profile_store.active()
            revision = profile_store.revision()
        except Exception:
            pass  # Fall back to Chroma default

        if active and active.backend == "milvus":
            key = f"milvus:{active.uri}:{active.id}:r{revision}"
            with _lock:
                _evict_stale(key)
                repo = _cache.get(key)
                if repo is None:
                    repo = MilvusVectorRepository.from_profile(active)
                    _cache[key] = repo
                return repo

        # Default / Chroma
        key = f"chroma:{settings.chroma_dir}:r{revision}"
        with _lock:
            _evict_stale(key)
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

    @staticmethod
    def invalidate() -> None:
        """Close and evict stale entries after a profile activation.

        Called by the migration coordinator right after the atomic
        ``activate_migration`` transaction increments the revision.
        """
        with _lock:
            for repo in _cache.values():
                try:
                    repo.close()
                except Exception:
                    pass
            _cache.clear()


def _evict_stale(new_key: str) -> None:
    """If the cache has entries for a different revision of the same backend,
    close and remove them so only the new revision's adapter is cached.

    Must be called under ``_lock``.
    """
    # Extract backend prefix (everything before :r)
    if ":r" in new_key:
        prefix = new_key.rsplit(":r", 1)[0]
    else:
        return
    stale_keys = [
        k for k in _cache
        if k.startswith(prefix + ":r") and k != new_key
    ]
    for k in stale_keys:
        try:
            _cache[k].close()
        except Exception:
            pass
        del _cache[k]
