# Milvus Profiles and Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add versioned Chroma/Milvus profiles, a production PyMilvus adapter, secure profile APIs, health checks, and a Settings UI that can save a pending remote database without activating it.

**Architecture:** Profile state in settings SQLite is authoritative for backend selection. The factory cache key expands to profile/revision, while a direct `MilvusClient` adapter implements the Plan 1 repository contract and dedicated APIs validate profiles without exposing credentials.

**Tech Stack:** Python 3.12, FastAPI, PyMilvus, SQLite, Pydantic, React 19, Next.js 16, TypeScript, pytest, Ruff, ESLint

## Global Constraints

- Requires completed Plan 1 interfaces from `2026-08-09-vector-storage-foundation.md`.
- Existing installations bootstrap their actual active generation as one active Chroma profile without rebuilding.
- Pending profiles never serve production reads/writes and cannot activate in this plan.
- Profile metric is L2 for existing Chroma and COSINE only when both source domains are empty.
- Credentials are write-only and redacted from API responses, validation errors, URIs, and logs.
- Milvus uses explicit schemas, HNSW (`M=16`, `efConstruction=200`, search `ef=128`), collection load, flush, and Strong-consistency verification.
- Collection base names match `^[A-Za-z][A-Za-z0-9_]{2,50}$`.
- Update `backend/uv.lock` only in the explicit dependency task; preserve unrelated user changes by inspecting its diff before and after.
- Commit messages are English.

## File Structure

- Create `backend/src/porto_chatbot/vector_profiles.py`: profile/state persistence, fingerprints, bootstrap.
- Create `backend/src/porto_chatbot/vector_stores/milvus.py`: PyMilvus repository.
- Create `backend/src/porto_chatbot/api/routes/vector_databases.py`: test/list/create/update/delete endpoints.
- Create `backend/src/porto_chatbot/models/vector_database.py`: profile request/response models.
- Modify `backend/src/porto_chatbot/vector_stores/factory.py`: `(data_dir, profile_id, revision)` lifecycle.
- Modify `backend/src/porto_chatbot/health.py`, `models/enums.py`, `api/app.py`, `api/deps.py`, `pyproject.toml`, `uv.lock`.
- Modify `frontend/src/lib/types.ts`, `frontend/src/lib/api.ts`, `frontend/src/components/porto-workbench.tsx`.
- Create backend profile, Milvus, API, security, and health tests.

---

### Task 1: Persist and Bootstrap Vector Profiles

**Files:**
- Create: `backend/src/porto_chatbot/vector_profiles.py`
- Create: `backend/src/porto_chatbot/models/vector_database.py`
- Create: `backend/tests/test_vector_profiles.py`

**Interfaces:**
- Produces: `VectorBackend`, `AuthMode`, `VectorProfile` including `metric`, `VectorStoreState`, `VectorProfileStore.bootstrap()`, `active()`, `pending()`, `save_pending()`, `update()`, `delete_inactive()`.

- [ ] **Step 1: Write failing persistence and bootstrap tests**

Assert legacy generation state bootstraps one active Chroma profile with L2 metric and its resolved collection; revision starts at 1; secret omission preserves the secret; explicit clear removes it; storage identity excludes timeout/credential rotation but includes backend/URI/database/TLS/collection bases/metric.

- [ ] **Step 2: Run the focused tests**

Run: `cd backend && uv run pytest tests/test_vector_profiles.py -q`
Expected: FAIL because profile storage does not exist.

- [ ] **Step 3: Implement SQLite tables and models**

Enable WAL and `PRAGMA busy_timeout=5000`; persist one state row; validate profile names, URI without userinfo/fragment, and collection base names; expose only `credential_configured` in response models.

- [ ] **Step 4: Implement idempotent compatibility bootstrap**

Read Plan 1's generation pointer and actual physical collection; use unsuffixed names only when no generation state exists. Never rebuild data.

- [ ] **Step 5: Run tests and Ruff**

Run: `cd backend && uv run pytest tests/test_vector_profiles.py tests/test_kb_generations.py -q && uv run ruff check src/porto_chatbot/vector_profiles.py src/porto_chatbot/models/vector_database.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/src/porto_chatbot/vector_profiles.py backend/src/porto_chatbot/models/vector_database.py backend/tests/test_vector_profiles.py
git commit -m "feat(vector-db): persist versioned storage profiles"
```

### Task 2: Add PyMilvus and Implement the Milvus Repository

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Create: `backend/src/porto_chatbot/vector_stores/milvus.py`
- Create: `backend/tests/test_milvus_repository.py`
- Create: `backend/tests/integration/test_milvus_repository_integration.py`

**Interfaces:**
- Consumes: Plan 1 repository contract.
- Produces: `MilvusVectorRepository.from_profile(profile: VectorProfile)` with explicit schema/index/load/upsert/search/fetch/export/count/delete/health/close; Chroma gains the same `from_profile` constructor in Task 3.

- [ ] **Step 1: Record the existing lockfile diff and add the dependency**

Run: `git --no-pager diff -- backend/uv.lock`
Then add `pymilvus>=2.6,<3.0` to `pyproject.toml` and run `cd backend && uv lock`.
Expected: the regenerated diff contains PyMilvus dependency changes plus the user's pre-existing changes, with none reverted.

- [ ] **Step 2: Write failing mocked-client adapter tests**

Cover URI/token/user-password construction, explicit KB/Memory schemas, 32,000-byte content and scalar limits, L2/COSINE HNSW parameters, load, upsert, filter templates with `filter_params`, stable primary-key export, flush/Strong reads, and typed exception mapping.

- [ ] **Step 3: Run tests and confirm the adapter is missing**

Run: `cd backend && uv run pytest tests/test_milvus_repository.py -q`
Expected: FAIL on missing module.

- [ ] **Step 4: Implement `MilvusVectorRepository` using `MilvusClient`**

Never interpolate `session_id`; reject oversized scalar/JSON fields before sending; preserve canonical scalar types; use collection metric from `CollectionSpec` and normalize returned scores through Plan 1 helpers.

- [ ] **Step 5: Add opt-in real integration tests**

Mark tests `integration`; skip unless `MILVUS_URI` exists; create unique collections and delete only those test collections in fixture teardown. Parameterize Plan 1's shared repository contract fixture so Milvus runs the same behavior suite when configured.

- [ ] **Step 6: Run unit tests and Ruff**

Run: `cd backend && uv run pytest tests/test_milvus_repository.py -q && uv run ruff check src/porto_chatbot/vector_stores/milvus.py tests/test_milvus_repository.py`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/pyproject.toml backend/uv.lock backend/src/porto_chatbot/vector_stores/milvus.py backend/tests/test_milvus_repository.py backend/tests/integration/test_milvus_repository_integration.py backend/tests/test_vector_repository_contract.py
git commit -m "feat(vector-db): add PyMilvus repository adapter"
```

### Task 3: Make KB Generations and Physical Names Profile Aware

**Files:**
- Modify: `backend/src/porto_chatbot/kb_generation_store.py`
- Modify: `backend/src/porto_chatbot/bm25_index.py`
- Modify: `backend/src/porto_chatbot/index_supervisor.py`
- Create: `backend/src/porto_chatbot/vector_stores/naming.py`
- Modify: `backend/tests/test_kb_generations.py`

**Interfaces:**
- Produces: `resolve_kb_collection(profile_revision, generation) -> str`, `resolve_memory_collection(profile_revision) -> str`; `KbGenerationStore.allocate(profile_id)`, `activate(profile_id, ...)`, `current(profile_id)`.

- [ ] **Step 1: Write failing profile-generation and naming tests**

Assert legacy names remain readable, profile revision 4 generation 7 resolves to `porto_kb__r4_g7`, Memory resolves to `porto_memory__r4`, BM25 cache keys include profile ID, and two profiles can hold different active generations.

- [ ] **Step 2: Run focused tests**

Run: `cd backend && uv run pytest tests/test_kb_generations.py tests/test_bm25_index.py -q`
Expected: FAIL because generation state is global.

- [ ] **Step 3: Implement profile-scoped generation rows and naming helpers**

Store resolved collection/path values. Reserve the target's post-activation revision (`current revision + 1`) when creating migration collections so Plan 3 activation does not rename data.

- [ ] **Step 4: Update supervisor and BM25 consumers**

Pass active profile ID through allocation/current lookup and use `(data_dir, profile_id, generation)` cache keys.

- [ ] **Step 5: Run tests and Ruff**

Run: `cd backend && uv run pytest tests/test_kb_generations.py tests/test_bm25_index.py tests/test_vector_store.py -q && uv run ruff check src/porto_chatbot/kb_generation_store.py src/porto_chatbot/vector_stores/naming.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/src/porto_chatbot/kb_generation_store.py backend/src/porto_chatbot/bm25_index.py backend/src/porto_chatbot/index_supervisor.py backend/src/porto_chatbot/vector_stores/naming.py backend/tests
git commit -m "feat(vector-db): scope KB generations by profile"
```

### Task 4: Make the Factory Profile and Revision Aware

**Files:**
- Modify: `backend/src/porto_chatbot/vector_stores/factory.py`
- Modify: `backend/src/porto_chatbot/api/deps.py`
- Modify: `backend/src/porto_chatbot/api/app.py`
- Create: `backend/tests/test_vector_repository_factory.py`

**Interfaces:**
- Produces cache key `(data_dir, profile_id, revision)` and `invalidate_stale(active_key)`; supports Chroma and Milvus through identical `from_profile(profile)` constructors.

- [ ] **Step 1: Write failing lifecycle tests**

Assert active Chroma resolves first; pending Milvus never resolves for production; changing only pending state does not replace active client; incrementing revision closes the old client once and constructs the new backend.

- [ ] **Step 2: Run tests**

Run: `cd backend && uv run pytest tests/test_vector_repository_factory.py -q`
Expected: FAIL because factory is data-dir keyed.

- [ ] **Step 3: Implement profile/revision resolution and shutdown cleanup**

Add `ChromaVectorRepository.from_profile(profile)`, bootstrap profiles before constructing services, construct both adapters through `from_profile`, inject only active profile, and close all clients in FastAPI lifespan shutdown.

- [ ] **Step 4: Run lifecycle and API regressions**

Run: `cd backend && uv run pytest tests/test_vector_repository_factory.py tests/test_api.py tests/test_agent_backend_injection.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/vector_stores/factory.py backend/src/porto_chatbot/api backend/tests
git commit -m "feat(runtime): resolve active vector profile by revision"
```

### Task 5: Add Secure Profile APIs and Health

**Files:**
- Create: `backend/src/porto_chatbot/api/routes/vector_databases.py`
- Modify: `backend/src/porto_chatbot/api/app.py`
- Modify: `backend/src/porto_chatbot/api/deps.py`
- Modify: `backend/src/porto_chatbot/health.py`
- Modify: `backend/src/porto_chatbot/models/enums.py`
- Create: `backend/tests/test_vector_databases_api.py`
- Modify: `backend/tests/test_health_embedding.py`

**Interfaces:**
- Produces endpoints from spec §11 except migration endpoints; save returns `migration_required`; `POST /test` returns coarse status codes.

- [ ] **Step 1: Write failing API/security tests**

Test list/test/create/update/confirmed inactive delete; secrets never appear in JSON, logs, or validation detail; reject cross-origin writes, wildcard CORS, URI userinfo/fragments/invalid schemes, active deletion, detailed network exceptions, and Milvus profiles whose effective RAG chunk limit can exceed the 65,535-byte scalar ceiling.

- [ ] **Step 2: Run API tests**

Run: `cd backend && uv run pytest tests/test_vector_databases_api.py tests/test_health_embedding.py -q`
Expected: FAIL because routes and dependency enum are missing.

- [ ] **Step 3: Implement routes, same-origin guard, explicit CORS, and coarse errors**

Allow configured Porto origins only. Test capability with a uniquely named probe collection and clean it up; cleanup failure is logged without exposing the secret.

- [ ] **Step 4: Add active `vector_database` health**

Pending test state is separate; feature availability derives only from active repository health and existing index readiness.

- [ ] **Step 5: Run API/security tests and Ruff**

Run: `cd backend && uv run pytest tests/test_vector_databases_api.py tests/test_health_embedding.py tests/test_api.py -q && uv run ruff check src tests/test_vector_databases_api.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/src/porto_chatbot/api backend/src/porto_chatbot/health.py backend/src/porto_chatbot/models/enums.py backend/tests
git commit -m "feat(api): add secure vector database profiles"
```

### Task 6: Add Vector Database Settings UI

**Files:**
- Modify: `frontend/src/lib/types.ts`
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/components/porto-workbench.tsx`

**Interfaces:**
- Produces: `VectorProfile`, `VectorProfileDraft`, list/test/save/delete API clients, and `SettingsSection` value `vector_database`.

- [ ] **Step 1: Add exact TypeScript API types**

Model `backend`, auth mode, redacted endpoint, `credential_configured`, active/pending state, connection result, and save response. Credential is request-only and absent from response types.

- [ ] **Step 2: Implement API clients and Settings navigation**

Add active/pending badges, segmented Chroma/Milvus selector, conditional URI/database/auth/TLS/timeout/collection fields, password input, Test Connection, Save, and confirmed inactive delete.

- [ ] **Step 3: Implement Plan 2 pending UX**

When save returns `migration_required`, show a persistent pending banner with `Migration support will be enabled in the next release`; do not render **Migrate now** until Plan 3.

- [ ] **Step 4: Run frontend lint and build**

Run: `cd frontend && npm run lint && npm run build`
Expected: PASS with no TypeScript or layout errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib frontend/src/components/porto-workbench.tsx
git commit -m "feat(frontend): configure vector database profiles"
```

### Task 7: Profiles and Milvus Verification

- [ ] **Step 1: Run backend unit suite and lint**

Run: `cd backend && uv run pytest -q && uv run ruff check src tests`
Expected: PASS.

- [ ] **Step 2: Run optional Milvus integration tests when configured**

Run: `cd backend && uv run pytest -m integration tests/integration/test_milvus_repository_integration.py -q`
Expected: PASS when `MILVUS_URI` is configured; otherwise SKIP with an explicit reason.

- [ ] **Step 3: Run frontend checks**

Run: `cd frontend && npm run lint && npm run build`
Expected: PASS.

- [ ] **Step 4: Update deployment documentation**

Document `MILVUS_URI`, optional token/user credentials, database, TLS, outbound allowlist, integration-test variables, and the fact that activation arrives in Plan 3.

- [ ] **Step 5: Commit**

```bash
git add backend/src backend/tests backend/pyproject.toml frontend/src docs/deployment.md
git commit -m "docs(milvus): document remote vector database setup"
```
