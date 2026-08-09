"""Migration coordinator — orchestrates copy, catch-up, verification.

Runs in a background thread.  The coordinator:
1. Copies KB snapshot + Memory records to the target profile.
2. Catches up Memory records written during the copy.
3. Raises a finalization barrier (blocks new vector writes).
4. Performs a final catch-up under the barrier.
5. Verifies counts and samples.
6. Atomically activates the target profile.
7. Releases the barrier and replays barrier-period intents to the target.
"""
from __future__ import annotations

import threading
from typing import Any

from ..kb_generation_store import KbGenerationStore
from ..logging_utils import get_component_logger
from ..memory.vector_delivery import VectorDeliveryStore
from ..settings import Settings
from ..vector_profiles import VectorProfile, VectorProfileStore
from ..vector_stores import (
    ChromaVectorRepository,
    CollectionSpec,
    DistanceMetric,
    MilvusVectorRepository,
    VectorRepository,
)
from ..vector_stores.factory import VectorRepositoryFactory
from .barrier import _BarrierGuard
from .store import MigrationPhase, MigrationStatus, VectorMigration, VectorMigrationStore

logger = get_component_logger("migration_coordinator")

_BATCH_SIZE = 500
_SAMPLE_SIZE = 20
_TOP_K = 10
_OVERLAP_THRESHOLD = 0.80
_BARRIER_TIMEOUT = 30.0


def _create_repo(profile: VectorProfile, settings: Settings) -> VectorRepository:
    """Create a repository for the given profile."""
    if profile.backend == "milvus":
        return MilvusVectorRepository.from_profile(profile)
    return ChromaVectorRepository(settings.chroma_dir)


class VectorMigrationCoordinator:
    """Coordinates the migration lifecycle for one data directory."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._mig_store = VectorMigrationStore(settings)
        self._profile_store = VectorProfileStore(settings)
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()

    # ── lifecycle ──

    def start_worker(self) -> None:
        """Start the background worker thread (if not already running)."""
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._run, name="migration-coordinator", daemon=True,
        )
        self._worker.start()
        logger.info("migration coordinator worker started")

    def stop_worker(self) -> None:
        """Signal the worker to stop and join."""
        self._stop.set()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=10.0)
        self._worker = None
        logger.info("migration coordinator worker stopped")

    # ── public API ──

    def submit(self) -> VectorMigration:
        """Start a migration from the active profile to the pending profile."""
        self._profile_store.bootstrap()
        active = self._profile_store.active()
        pending = self._profile_store.pending()
        if active is None:
            raise ValueError("no active profile")
        if pending is None:
            raise ValueError("no pending profile")

        mig = self._mig_store.create(
            source_profile_id=active.id,
            target_profile_id=pending.id,
        )
        self.start_worker()
        return mig

    def get_status(self) -> VectorMigration | None:
        return self._mig_store.current_or_latest()

    def cancel(self) -> None:
        mig = self._mig_store.current()
        if mig is not None:
            self._mig_store.cancel(mig.id)

    # ── worker ──

    def _run(self) -> None:
        mig = self._mig_store.current()
        if mig is None:
            return
        try:
            self._execute(mig)
        except Exception as exc:
            logger.exception("migration worker crashed")
            self._mig_store.transition(
                mig.id, status=MigrationStatus.FAILED, error=str(exc)[:200],
            )

    def _execute(self, mig: VectorMigration) -> None:
        source_profile = self._profile_store.get(mig.source_profile_id)
        target_profile = self._profile_store.get(mig.target_profile_id)
        if source_profile is None or target_profile is None:
            self._mig_store.transition(
                mig.id, status=MigrationStatus.FAILED, error="profile not found",
            )
            return

        source_repo = _create_repo(source_profile, self._settings)
        target_repo = _create_repo(target_profile, self._settings)

        try:
            # Phase 1: Copy KB
            self._copy_kb(mig, source_repo, target_repo, source_profile, target_profile)

            # Phase 2: Copy Memory
            self._copy_memory(mig, source_repo, target_repo, source_profile, target_profile)

            # Phase 3: Catch-up (before barrier)
            self._mig_store.transition(mig.id, phase=MigrationPhase.CATCHING_UP)
            self._catchup_memory(mig, source_repo, target_repo, source_profile, target_profile)

            # Phase 4: Finalize — raise barrier, final catch-up, verify, activate
            self._mig_store.transition(mig.id, phase=MigrationPhase.FINALIZING)
            self._finalize_and_activate(
                mig, source_repo, target_repo, source_profile, target_profile,
            )

            self._mig_store.transition(mig.id, status=MigrationStatus.SUCCEEDED)
            logger.info("migration succeeded id=%s", mig.id)
        except Exception as exc:
            self._mig_store.transition(
                mig.id, status=MigrationStatus.FAILED, error=str(exc)[:200],
            )
            raise
        finally:
            target_repo.close()
            source_repo.close()

    # ── copy KB ──

    def _copy_kb(
        self, mig: VectorMigration,
        source_repo: VectorRepository, target_repo: VectorRepository,
        source_profile: VectorProfile, target_profile: VectorProfile,
    ) -> None:
        """Copy the KB collection snapshot from source to target."""
        gen_store = KbGenerationStore(self._settings)
        gen = gen_store.current(profile_id=source_profile.id)
        if gen is None:
            gen = gen_store.current()  # backward compat

        source_collection = gen.collection_name if gen else source_profile.kb_collection_base
        target_collection = self._target_kb_collection(target_profile)

        # Ensure target collection exists
        spec = self._kb_collection_spec(target_profile, source_collection, source_repo)
        target_repo.ensure_collection(spec)

        copied = 0
        total = source_repo.count(source_collection)
        self._mig_store.update_progress(mig.id, kb_total=total)
        self._mig_store.update_progress(mig.id, kb_generation=gen.id if gen else 1)

        for batch in source_repo.export_batches(source_collection, _BATCH_SIZE):
            if self._stop.is_set():
                return
            target_repo.upsert(target_collection, batch)
            copied += len(batch)
            self._mig_store.update_progress(mig.id, kb_copied=copied)
            self._mig_store.heartbeat(mig.id)

        logger.info("KB copy done source=%s target=%s copied=%s", source_collection, target_collection, copied)

    def _target_kb_collection(self, target_profile: VectorProfile) -> str:
        """Resolve the target KB collection name using the next revision."""
        revision = self._profile_store.revision() + 1
        from ..vector_stores.naming import resolve_kb_collection
        return resolve_kb_collection(target_profile.kb_collection_base, revision, 1)

    def _kb_collection_spec(
        self, target_profile: VectorProfile, source_collection: str,
        source_repo: VectorRepository,
    ) -> CollectionSpec:
        """Build a CollectionSpec for the target matching source dimensions."""
        info = source_repo.collection_info(source_collection)
        metric = DistanceMetric.COSINE if target_profile.metric == "cosine" else DistanceMetric.L2
        return CollectionSpec(
            name=self._target_kb_collection(target_profile),
            dimension=info.dimension or self._settings.embedding_dimensions,
            metric=metric,
            metadata={"kind": "kb"},
        )

    # ── copy memory ──

    def _copy_memory(
        self, mig: VectorMigration,
        source_repo: VectorRepository, target_repo: VectorRepository,
        source_profile: VectorProfile, target_profile: VectorProfile,
    ) -> None:
        """Copy Memory vectors from source to target."""
        source_collection = source_profile.memory_collection_base
        target_collection = self._target_memory_collection(target_profile)

        # Ensure target collection exists
        info = source_repo.collection_info(source_collection)
        metric = DistanceMetric.COSINE if target_profile.metric == "cosine" else DistanceMetric.L2
        target_repo.ensure_collection(CollectionSpec(
            name=target_collection,
            dimension=info.dimension or self._settings.embedding_dimensions,
            metric=metric,
            metadata={"kind": "memory"},
        ))

        copied = 0
        total = source_repo.count(source_collection)
        self._mig_store.update_progress(mig.id, memory_total=total)

        for batch in source_repo.export_batches(source_collection, _BATCH_SIZE):
            if self._stop.is_set():
                return
            target_repo.upsert(target_collection, batch)
            copied += len(batch)
            self._mig_store.update_progress(mig.id, memory_copied=copied)
            self._mig_store.heartbeat(mig.id)

        # Track memory sequence for catch-up
        delivery = VectorDeliveryStore(self._settings)
        max_seq = delivery.max_seq()
        self._mig_store.update_progress(mig.id, memory_seq=max_seq)

        logger.info("memory copy done source=%s target=%s copied=%s seq=%s",
                     source_collection, target_collection, copied, max_seq)

    def _target_memory_collection(self, target_profile: VectorProfile) -> str:
        revision = self._profile_store.revision() + 1
        from ..vector_stores.naming import resolve_memory_collection
        return resolve_memory_collection(target_profile.memory_collection_base, revision)

    # ── catch-up (pre-barrier) ──

    def _catchup_memory(
        self, mig: VectorMigration,
        source_repo: VectorRepository, target_repo: VectorRepository,
        source_profile: VectorProfile, target_profile: VectorProfile,
    ) -> None:
        """Copy Memory records that arrived after the initial copy snapshot."""
        delivery = VectorDeliveryStore(self._settings)
        pending = delivery.pending_after(mig.memory_seq, source_profile.id)

        if not pending:
            logger.info("memory catch-up: no pending records")
            return

        source_collection = source_profile.memory_collection_base
        target_collection = self._target_memory_collection(target_profile)

        records = source_repo.fetch(source_collection, pending)
        if records:
            target_repo.upsert(target_collection, records)
            logger.info("memory catch-up copied=%s", len(records))

        # Update sequence
        new_max = delivery.max_seq()
        self._mig_store.update_progress(mig.id, memory_seq=new_max)

    # ── finalize + activate ──

    def _finalize_and_activate(
        self, mig: VectorMigration,
        source_repo: VectorRepository, target_repo: VectorRepository,
        source_profile: VectorProfile, target_profile: VectorProfile,
    ) -> None:
        """Raise barrier, final catch-up, verify, activate, release, replay.

        The barrier blocks new vector writes and KB rebuild submissions
        for the duration of final catch-up + verification + activation.
        On any failure the barrier is released and the source remains active.
        """
        with _BarrierGuard(timeout=_BARRIER_TIMEOUT):
            # Final Memory catch-up under the barrier.
            self._final_catchup(mig, source_repo, target_repo, source_profile)

            # Confirm KB generation has not changed.
            self._confirm_kb_generation(mig, source_profile)

            # Verify (still under barrier).
            self._mig_store.transition(mig.id, phase=MigrationPhase.VERIFYING)
            self._verify(mig, source_repo, target_repo, source_profile, target_profile)

            # Atomic activation (still under barrier).
            self._mig_store.transition(mig.id, phase=MigrationPhase.ACTIVATING)
            new_revision = self._profile_store.activate_migration(
                source_profile_id=mig.source_profile_id,
                target_profile_id=mig.target_profile_id,
            )
            VectorRepositoryFactory.invalidate()
            logger.info("factory invalidated after activation revision=%s", new_revision)

        # Post-activation replay: index barrier-period intents to target.
        self._replay_barrier_intents(mig, target_profile)

    def _final_catchup(
        self, mig: VectorMigration,
        source_repo: VectorRepository, target_repo: VectorRepository,
        source_profile: VectorProfile,
    ) -> None:
        """Copy Memory records that arrived during the bulk copy phase.

        Under the barrier, no new vector writes can occur, so this catch-up
        captures the last source receipts through the current max seq.
        """
        delivery = VectorDeliveryStore(self._settings)
        pending = delivery.pending_after(mig.memory_seq, source_profile.id)

        if not pending:
            logger.info("final catch-up: no pending records")
            return

        source_collection = source_profile.memory_collection_base
        target_collection = self._target_memory_collection(
            self._profile_store.get(mig.target_profile_id),  # type: ignore[arg-type]
        )

        records = source_repo.fetch(source_collection, pending)
        if records:
            target_repo.upsert(target_collection, records)
            logger.info("final catch-up copied=%s", len(records))

        new_max = delivery.max_seq()
        self._mig_store.update_progress(mig.id, memory_seq=new_max)

    def _confirm_kb_generation(
        self, mig: VectorMigration, source_profile: VectorProfile,
    ) -> None:
        """Verify the source KB generation has not changed during migration."""
        gen_store = KbGenerationStore(self._settings)
        current_gen = gen_store.current(profile_id=source_profile.id)
        if current_gen is None:
            current_gen = gen_store.current()
        current_gen_id = current_gen.id if current_gen else 1

        if mig.kb_generation is not None and current_gen_id != mig.kb_generation:
            raise RuntimeError(
                f"kb_generation_unstable: expected {mig.kb_generation}, "
                f"got {current_gen_id}",
            )

    # ── verify ──

    def _verify(
        self, mig: VectorMigration,
        source_repo: VectorRepository, target_repo: VectorRepository,
        source_profile: VectorProfile, target_profile: VectorProfile,
    ) -> None:
        """Verify counts match between source and target."""
        import json

        results: dict[str, Any] = {}

        # KB count check
        gen_store = KbGenerationStore(self._settings)
        gen = gen_store.current() or gen_store.current(profile_id=source_profile.id)
        source_kb = gen.collection_name if gen else source_profile.kb_collection_base
        target_kb = self._target_kb_collection(target_profile)
        source_kb_count = source_repo.count(source_kb)
        target_kb_count = target_repo.count(target_kb)
        results["kb_count_match"] = source_kb_count == target_kb_count
        results["kb_source_count"] = source_kb_count
        results["kb_target_count"] = target_kb_count

        # Memory count check
        source_mem = source_profile.memory_collection_base
        target_mem = self._target_memory_collection(target_profile)
        source_mem_count = source_repo.count(source_mem)
        target_mem_count = target_repo.count(target_mem)
        results["memory_count_match"] = source_mem_count == target_mem_count
        results["memory_source_count"] = source_mem_count
        results["memory_target_count"] = target_mem_count

        self._mig_store.save_verification(mig.id, json.dumps(results))

        if not results["kb_count_match"] or not results["memory_count_match"]:
            raise RuntimeError(
                f"verification failed: {json.dumps(results)}",
            )

        logger.info("verification passed: %s", json.dumps(results))

    # ── post-activation replay ──

    def _replay_barrier_intents(
        self, mig: VectorMigration, target_profile: VectorProfile,
    ) -> None:
        """Index intents that arrived during the barrier into the target.

        After activation, any messages whose intent was created during the
        barrier (and therefore never received a source receipt for the
        target profile) are replayed.  This is independent of the
        migration's terminal status.
        """
        delivery = VectorDeliveryStore(self._settings)
        # Find all intents lacking a receipt for the target profile.
        missing = delivery.source_records_through(
            delivery.max_seq(), target_profile.id,
        )
        if not missing:
            logger.info("barrier replay: no pending intents")
            return

        # Fetch from the now-active target repo (post-activation).
        repo = VectorRepositoryFactory.get(self._settings)
        target_collection = target_profile.memory_collection_base

        try:
            records = repo.fetch(target_collection, missing)
            if records:
                repo.upsert(target_collection, records)
                delivery.mark_delivered(
                    [r.id for r in records], target_profile.id,
                )
                logger.info("barrier replay indexed=%s", len(records))
        except Exception:
            logger.exception("barrier replay failed")
