# Online Vector Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate KB and Memory together between Chroma and Milvus profiles while the old profile serves traffic, then verify and atomically activate the target with migrate-now/migrate-later UX.

**Architecture:** SQLite index intents/receipts define Memory delivery truth, KB generation pointers define immutable snapshots, and one persistent migration coordinator performs copy, catch-up, a bounded finalization barrier, verification, and an atomic profile/generation/revision switch.

**Tech Stack:** Python 3.12, FastAPI, SQLite, ChromaDB, PyMilvus, React 19, Next.js 16, pytest, Ruff, ESLint

## Global Constraints

- Requires completed Plans 1 and 2.
- Only the active profile serves reads/writes before activation.
- KB and Memory activate together; any failure leaves the source profile active.
- Memory migration copies only source-profile receipts created from explicit index intents; it never scans all messages or re-embeds migrated records.
- Migration lock is acquired before `RAG_INDEX_LOCK`; reverse acquisition is forbidden.
- Defaults: batch 500, operation timeout 30s, retry 3, barrier 30s, stale heartbeat 90s, 20 samples, Top-10, 80% overlap, at most 2 KB generation restarts.
- Activation is one settings-SQLite transaction covering profile, per-profile KB/BM25 pointers, revision, and migration status.
- Cancel never deletes data; cleanup requires confirmed inactive-profile deletion.
- Commit messages are English.

## File Structure

- Create `backend/src/porto_chatbot/memory/vector_delivery.py`: intent/receipt persistence and replay queries.
- Create `backend/src/porto_chatbot/vector_migrations/store.py`: migration records and transitions.
- Create `backend/src/porto_chatbot/vector_migrations/coordinator.py`: worker/state machine.
- Create `backend/src/porto_chatbot/vector_migrations/verification.py`: counts/digests/samples/metadata checks.
- Create `backend/src/porto_chatbot/vector_migrations/__init__.py`.
- Create `backend/src/porto_chatbot/api/routes/vector_migrations.py`.
- Modify Memory persistence, `IndexSupervisor`, locks, app lifespan, deps, profile store, and vector database save API.
- Modify frontend API/types/settings UI for modal/banner/progress/retry/cancel.
- Add focused persistence, coordinator, API, startup recovery, and workflow tests.

---

### Task 1: Add Memory Index Intents and Profile Receipts

**Files:**
- Create: `backend/src/porto_chatbot/memory/vector_delivery.py`
- Modify: `backend/src/porto_chatbot/memory/session_store.py`
- Modify: `backend/src/porto_chatbot/memory/persist.py`
- Modify: `backend/src/porto_chatbot/models/chat.py`
- Create: `backend/tests/memory/test_vector_delivery.py`
- Modify: `backend/tests/memory/test_persist.py`

**Interfaces:**
- Produces: `VectorIndexIntent(seq, record_id, created_at)`, `VectorDeliveryStore.create_intent()`, `mark_delivered()`, `max_seq()`, `source_records_through()`, `pending_after()`.

- [ ] **Step 1: Write failing transactional delivery tests**

Assert `index_vector=True` persists message+intent transactionally; `False` creates no intent; receipt appears only after repository upsert; failed upsert leaves intent without receipt; bootstrap receipts are created only for IDs exported from the active source collection.

- [ ] **Step 2: Run focused tests**

Run: `cd backend && uv run pytest tests/memory/test_vector_delivery.py tests/memory/test_persist.py -q`
Expected: FAIL because delivery tables do not exist.

- [ ] **Step 3: Implement tables in `memory.sqlite3`**

Use `seq INTEGER PRIMARY KEY AUTOINCREMENT`, unique `record_id`, and receipt primary key `(record_id, profile_id)`. Keep `messages.indexed` synchronized only as a compatibility projection.

- [ ] **Step 4: Integrate intents and receipts into all chat persistence paths**

Search `index_vector=` call sites and preserve existing DIRECT/chitchat exclusion exactly. On vector failure, keep message and intent but omit receipt.

- [ ] **Step 5: Run Memory and chat tests**

Run: `cd backend && uv run pytest tests/memory tests/test_chat_dispatch.py tests/test_agent_sdk_chat.py -q && uv run ruff check src/porto_chatbot/memory`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/src/porto_chatbot/memory backend/src/porto_chatbot/models/chat.py backend/tests
git commit -m "feat(memory): track profile-aware vector delivery"
```

### Task 2: Persist Migration State and Locking

**Files:**
- Create: `backend/src/porto_chatbot/vector_migrations/store.py`
- Create: `backend/src/porto_chatbot/vector_migrations/__init__.py`
- Modify: `backend/src/porto_chatbot/locking.py`
- Modify: `backend/src/porto_chatbot/vector_profiles.py`
- Create: `backend/tests/test_vector_migration_store.py`

**Interfaces:**
- Produces: `MigrationStatus`, `MigrationPhase`, `VectorMigration`, `VectorMigrationStore.create/get/current/transition/progress/interrupt_stale/cancel`; `VECTOR_MIGRATION_LOCK`.

- [ ] **Step 1: Write failing state-machine tests**

Assert one running migration per data directory; allowed transitions match the spec; activation cannot be cancelled; failed/interrupted/cancelled can retry; heartbeat older than 90 seconds becomes interrupted; errors are redacted.

- [ ] **Step 2: Run focused tests**

Run: `cd backend && uv run pytest tests/test_vector_migration_store.py -q`
Expected: FAIL because migration persistence is missing.

- [ ] **Step 3: Implement migration tables and guarded transitions**

Enable WAL/busy timeout; enforce uniqueness transactionally; store KB generation, Memory sequence, batch cursors, totals, verification JSON, timestamps, and coarse errors.

- [ ] **Step 4: Define and test lock ordering**

Migration code acquires `VECTOR_MIGRATION_LOCK` before `RAG_INDEX_LOCK`. Add a test that a reindex submission is rejected while finalization owns the migration lock.

- [ ] **Step 5: Run tests and Ruff**

Run: `cd backend && uv run pytest tests/test_vector_migration_store.py tests/test_node_backend_dispatch.py -q && uv run ruff check src/porto_chatbot/vector_migrations src/porto_chatbot/locking.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/src/porto_chatbot/vector_migrations backend/src/porto_chatbot/locking.py backend/src/porto_chatbot/vector_profiles.py backend/tests/test_vector_migration_store.py
git commit -m "feat(vector-migration): persist state and lock ordering"
```

### Task 3: Implement Copy, Catch-up, and Verification

**Files:**
- Create: `backend/src/porto_chatbot/vector_migrations/verification.py`
- Create: `backend/src/porto_chatbot/vector_migrations/coordinator.py`
- Modify: `backend/src/porto_chatbot/index_supervisor.py`
- Modify: `backend/src/porto_chatbot/bm25_index.py`
- Create: `backend/tests/test_vector_migration_coordinator.py`

**Interfaces:**
- Produces: `VectorMigrationCoordinator.submit/start/stop/status`; `verify_domain_ids`, `verify_round_trip_samples`, `verify_search_overlap`.

- [ ] **Step 1: Write failing happy-path and failure-path coordinator tests**

Use repository doubles for Chroma→Milvus, Milvus→Chroma, and Milvus→Milvus. Assert exact phase order, 500-record batches, idempotent retry, exact-vector catch-up, no unreceipted message migration, 20-sample/Top-10/80% gate, metadata hash checks, source-active-on-failure, and successful activation when both source domains are empty without prematurely creating vector fields.

- [ ] **Step 2: Run focused tests**

Run: `cd backend && uv run pytest tests/test_vector_migration_coordinator.py -q`
Expected: FAIL because coordinator is missing.

- [ ] **Step 3: Implement preflight, target preparation, and bulk copy**

Require matching embedding fingerprint/metric or explicit derived-fingerprint confirmation. Copy KB snapshot by generation and Memory records represented by source receipts through captured sequence. Persist progress only after successful upsert.

- [ ] **Step 4: Implement catch-up and KB generation restart**

Fetch exact source vectors for later receipted intents. If KB generation changes, rebuild only target KB/BM25; fail with `kb_generation_unstable` after two restarts.

- [ ] **Step 5: Implement Strong-consistency verification**

Flush destination; compare counts and ordered ID digest; sample canonical document/metadata hashes; run deterministic vector samples and require 80% ID overlap; verify BM25 count.

- [ ] **Step 6: Run coordinator tests and Ruff**

Run: `cd backend && uv run pytest tests/test_vector_migration_coordinator.py tests/test_kb_generations.py tests/test_bm25_index.py -q && uv run ruff check src/porto_chatbot/vector_migrations`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/src/porto_chatbot/vector_migrations backend/src/porto_chatbot/index_supervisor.py backend/src/porto_chatbot/bm25_index.py backend/tests
git commit -m "feat(vector-migration): copy and verify KB and Memory"
```

### Task 4: Add Finalization Barrier and Atomic Activation

**Files:**
- Modify: `backend/src/porto_chatbot/vector_migrations/coordinator.py`
- Modify: `backend/src/porto_chatbot/vector_profiles.py`
- Modify: `backend/src/porto_chatbot/vector_stores/factory.py`
- Modify: `backend/src/porto_chatbot/memory/persist.py`
- Modify: `backend/src/porto_chatbot/index_supervisor.py`
- Create: `backend/tests/test_vector_migration_activation.py`

**Interfaces:**
- Produces: `VectorProfileStore.activate_migration(...)` as one transaction; bounded barrier; post-activation receipt replay.

- [ ] **Step 1: Write failing concurrency/activation tests**

Assert barrier blocks vector writes and reindex submission but allows SQLite messages/intents; final catch-up includes last source receipt; timeout releases barrier; activation changes profile, KB/BM25 pointers, revision, and migration status atomically; stale client closes once; barrier intents replay to target afterward.

- [ ] **Step 2: Run focused tests**

Run: `cd backend && uv run pytest tests/test_vector_migration_activation.py -q`
Expected: FAIL because barrier and atomic activation are absent.

- [ ] **Step 3: Implement the 30-second finalization barrier**

Use the documented lock order. Do not hold a SQLite transaction while performing remote calls. Release all locks on timeout/error.

- [ ] **Step 4: Implement atomic activation and factory invalidation**

Inside one settings transaction verify expected source/status, set target active, clear pending, set target per-profile KB/BM25 pointers/resolved paths, increment revision, and mark succeeded.

- [ ] **Step 5: Implement post-activation replay**

Index intents lacking target receipts; preserve intent semantics and retry independently of completed migration status.

- [ ] **Step 6: Run activation and regression tests**

Run: `cd backend && uv run pytest tests/test_vector_migration_activation.py tests/test_vector_repository_factory.py tests/memory/test_vector_delivery.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/src/porto_chatbot/vector_migrations backend/src/porto_chatbot/vector_profiles.py backend/src/porto_chatbot/vector_stores/factory.py backend/src/porto_chatbot/memory/persist.py backend/src/porto_chatbot/index_supervisor.py backend/tests
git commit -m "feat(vector-migration): atomically activate migrated profiles"
```

### Task 5: Add Migration API and Startup Recovery

**Files:**
- Create: `backend/src/porto_chatbot/api/routes/vector_migrations.py`
- Modify: `backend/src/porto_chatbot/api/routes/vector_databases.py`
- Modify: `backend/src/porto_chatbot/api/app.py`
- Modify: `backend/src/porto_chatbot/api/deps.py`
- Create: `backend/tests/test_vector_migrations_api.py`
- Create: `backend/tests/test_vector_migration_recovery.py`

**Interfaces:**
- Produces spec §11 migration endpoints and save response ready for frontend modal.

- [ ] **Step 1: Write failing API and recovery tests**

Assert start only targets current pending profile; current/status survive refresh; retry/cancel enforce states; startup interrupts stale jobs and starts one worker; shutdown joins it; save returns `migration_required` only for storage identity changes.

- [ ] **Step 2: Run focused tests**

Run: `cd backend && uv run pytest tests/test_vector_migrations_api.py tests/test_vector_migration_recovery.py -q`
Expected: FAIL because routes/lifecycle are missing.

- [ ] **Step 3: Implement routes and dependency wiring**

Apply Plan 2 same-origin guard and coarse redacted errors. Do not expose credentials or provider exception text.

- [ ] **Step 4: Start/stop coordinator in FastAPI lifespan**

Mark stale jobs interrupted before worker start; keep `IndexSupervisor` lifecycle compatible.

- [ ] **Step 5: Run API/recovery tests and Ruff**

Run: `cd backend && uv run pytest tests/test_vector_migrations_api.py tests/test_vector_migration_recovery.py tests/test_api.py -q && uv run ruff check src tests/test_vector_migrations_api.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/src/porto_chatbot/api backend/src/porto_chatbot/vector_migrations backend/tests
git commit -m "feat(api): expose online vector migration lifecycle"
```

### Task 6: Add Migrate-now/Migrate-later UX and Progress

**Files:**
- Modify: `frontend/src/lib/types.ts`
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/components/porto-workbench.tsx`

**Interfaces:**
- Produces migration types/API clients, save modal, pending banner, progress, retry, and cancel.

- [ ] **Step 1: Add exact migration API types**

Model every status/phase, KB/Memory totals, copied counts, heartbeat, redacted error, source/target summaries, and allowed actions.

- [ ] **Step 2: Replace Plan 2 interim banner with the post-save modal**

On `migration_required`, show required copy and **Migrate now** / **Migrate later**. Later closes modal but keeps pending banner; now submits pending profile and opens progress.

- [ ] **Step 3: Add refresh-safe progress and actions**

Poll current migration, render phase-specific progress, heartbeat, retry on failed/interrupted/cancelled, and cancel only before activating. Successful activation refreshes active/pending profiles.

- [ ] **Step 4: Run frontend checks**

Run: `cd frontend && npm run lint && npm run build`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src
git commit -m "feat(frontend): guide online vector database migration"
```

### Task 7: End-to-End Verification

- [ ] **Step 1: Run complete backend tests and lint**

Run: `cd backend && uv run pytest -q && uv run ruff check src tests`
Expected: PASS.

- [ ] **Step 2: Run optional real Milvus migration integration**

Run: `cd backend && uv run pytest -m integration tests/integration -q`
Expected: PASS with `MILVUS_URI`; otherwise SKIP with explicit reasons.

- [ ] **Step 3: Run frontend checks**

Run: `cd frontend && npm run lint && npm run build`
Expected: PASS.

- [ ] **Step 4: Add migration operations documentation**

Document progress phases, migrate later, retry, cancel-vs-cleanup, source retention, rollback procedure, credential rotation, and recovery after interrupted jobs.

- [ ] **Step 5: Commit**

```bash
git add backend/src backend/tests frontend/src docs/deployment.md
git commit -m "docs(vector-migration): document migration operations"
```
