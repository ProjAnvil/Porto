# Vector Storage Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract Porto's Chroma-specific KB and Memory behavior behind a backend-neutral repository while preserving current retrieval ordering and adding safe KB/BM25 generations.

**Architecture:** Canonical vector records and a repository protocol sit below KB and Memory services. A Chroma adapter implements the protocol, a compatibility factory owns lifecycle, and generation state makes KB rebuilds prepare-then-switch rather than delete-in-place.

**Tech Stack:** Python 3.12, FastAPI, ChromaDB, LlamaIndex BM25, SQLite, pytest, Ruff

## Global Constraints

- Preserve existing Chroma L2 result ordering; adapters return higher-is-better scores using `exp(-distance)` for L2.
- Keep Memory message text canonical in SQLite and hydrate vector hits by message ID.
- Never reset an active Memory or KB collection on a dimension mismatch; use typed errors and graceful Memory degradation.
- Before profiles exist, repository clients are cached by `data_dir` only.
- KB rebuilds create immutable generation collections and BM25 directories, then atomically switch local pointers.
- Do not modify the user's pre-existing `backend/uv.lock` change in this plan.
- Commit messages are English.

## File Structure

- Create `backend/src/porto_chatbot/vector_stores/base.py`: canonical records, specs, hits, health, errors, and protocol.
- Create `backend/src/porto_chatbot/vector_stores/chroma.py`: Chroma repository implementation.
- Create `backend/src/porto_chatbot/vector_stores/factory.py`: compatibility repository cache and close/reset hooks.
- Create `backend/src/porto_chatbot/vector_stores/__init__.py`: public exports.
- Create `backend/src/porto_chatbot/kb_generation_store.py`: SQLite generation pointers and resolved paths.
- Modify `backend/src/porto_chatbot/vector_store.py`: KB application service over the repository.
- Modify `backend/src/porto_chatbot/retrieval.py`: repository hit/fetch-based retrieval.
- Modify `backend/src/porto_chatbot/bm25_index.py`: generation-aware paths and cache keys.
- Modify `backend/src/porto_chatbot/memory/conversation_memory.py`: repository-based indexing/search and SQLite hydration.
- Modify `backend/src/porto_chatbot/memory/session_store.py`: bulk message hydration.
- Modify `backend/src/porto_chatbot/api/deps.py`, `agent_sdk/backend.py`, `workflow_executor.py`, `agent/orchestrator.py`: factory construction points.
- Create `backend/tests/test_vector_repository_contract.py` and `backend/tests/test_kb_generations.py`.
- Modify `backend/tests/test_vector_store.py`, `backend/tests/test_memory.py` or nearest existing Memory tests, and dependency-injection tests.

---

### Task 1: Define the Vector Repository Contract and Chroma Adapter

**Files:**
- Create: `backend/src/porto_chatbot/vector_stores/base.py`
- Create: `backend/src/porto_chatbot/vector_stores/chroma.py`
- Create: `backend/src/porto_chatbot/vector_stores/__init__.py`
- Create: `backend/tests/test_vector_repository_contract.py`

**Interfaces:**
- Produces: `DistanceMetric`, `VectorRecord`, `CollectionSpec`, `CollectionInfo`, `VectorSearchRequest`, `VectorSearchHit`, `VectorStoreHealth`, `VectorRepository`, `DimensionMismatchError`, `CollectionNotReadyError`, `VectorStoreConnectionError`, `ChromaVectorRepository`.
- `export_batches` is snapshot-bounded and primary-key ascending.

- [ ] **Step 1: Write contract tests that fail before the package exists**

```python
@pytest.mark.parametrize("metric", [DistanceMetric.L2, DistanceMetric.COSINE])
def test_chroma_contract_round_trips_and_orders_records(tmp_path, metric):
    repo = ChromaVectorRepository(tmp_path / "chroma")
    spec = CollectionSpec(name="records", dimension=3, metric=metric, metadata={"kind": "kb"})
    repo.ensure_collection(spec)
    repo.upsert("records", [
        VectorRecord("b", [0.0, 1.0, 0.0], "beta", {"chunk": 2, "source_mtime": 1.5}),
        VectorRecord("a", [1.0, 0.0, 0.0], "alpha", {"chunk": 1, "source_mtime": 1.0}),
    ])
    assert [row.id for batch in repo.export_batches("records", 1) for row in batch] == ["a", "b"]
    assert repo.fetch("records", ["a"])[0].metadata == {"chunk": 1, "source_mtime": 1.0}
```

- [ ] **Step 2: Run the focused test and confirm import failure**

Run: `cd backend && uv run pytest tests/test_vector_repository_contract.py -q`
Expected: FAIL because `porto_chatbot.vector_stores` does not exist.

- [ ] **Step 3: Implement canonical dataclasses, protocol, typed errors, and Chroma operations**

Use frozen dataclasses for records/specs and a runtime-checkable protocol. Configure Chroma metric explicitly in new collections, map L2 score with `math.exp(-distance)`, and convert Chroma exceptions to Porto errors.

- [ ] **Step 4: Add contract cases for idempotent upsert, filters, count/delete, stable export, metadata types, and dimension mismatch**

During export, insert a third record after the first yielded batch and assert it is absent from the captured traversal. Assert a wrong-dimension upsert raises `DimensionMismatchError` without deleting the collection.

- [ ] **Step 5: Run contract tests and Ruff**

Run: `cd backend && uv run pytest tests/test_vector_repository_contract.py -q && uv run ruff check src/porto_chatbot/vector_stores tests/test_vector_repository_contract.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/src/porto_chatbot/vector_stores backend/tests/test_vector_repository_contract.py
git commit -m "refactor(vector-store): add repository contract and Chroma adapter"
```

### Task 2: Refactor KB Retrieval onto the Repository

**Files:**
- Modify: `backend/src/porto_chatbot/vector_store.py`
- Modify: `backend/src/porto_chatbot/retrieval.py`
- Modify: `backend/tests/test_vector_store.py`
- Modify: `backend/tests/test_rerankers.py`

**Interfaces:**
- Consumes: `VectorRepository.search`, `fetch`, `upsert`, and `CollectionSpec` from Task 1.
- Produces: existing `ChromaVectorStore`/`LocalVectorStore` public methods with unchanged call signatures; no raw Chroma collection escapes into `retrieval.py`.

- [ ] **Step 1: Add failing regression tests for L2 ranking and repository delegation**

Create a fake repository that records `VectorSearchRequest` and returns canonical hits. Assert `search`, BM25 fetch, hybrid fusion, stats, and `IndexStats.backend` use repository values without constructing `LlamaChromaVectorStore`.

- [ ] **Step 2: Run the focused tests**

Run: `cd backend && uv run pytest tests/test_vector_store.py tests/test_rerankers.py -q`
Expected: FAIL because KB retrieval still consumes a raw Chroma collection.

- [ ] **Step 3: Move vector search/fetch behind repository operations**

Keep document discovery, chunking, embedding, BM25 fusion, and reranking in the KB service. Convert chunks to `VectorRecord` at the write boundary and hits to `SourceChunk` at the read boundary.

- [ ] **Step 4: Remove the LlamaIndex Chroma adapter dependency from `retrieval.py`**

Keep RRF/hybrid behavior by feeding canonical vector hits and BM25 candidates into the existing fusion logic; preserve L2 ordering and current `top_k` behavior.

- [ ] **Step 5: Run KB tests and Ruff**

Run: `cd backend && uv run pytest tests/test_vector_store.py tests/test_bm25_index.py tests/test_rerankers.py -q && uv run ruff check src/porto_chatbot/vector_store.py src/porto_chatbot/retrieval.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/src/porto_chatbot/vector_store.py backend/src/porto_chatbot/retrieval.py backend/tests
git commit -m "refactor(rag): route KB retrieval through vector repository"
```

### Task 3: Refactor Conversation Memory with Safe Degradation and Hydration

**Files:**
- Modify: `backend/src/porto_chatbot/memory/conversation_memory.py`
- Modify: `backend/src/porto_chatbot/memory/session_store.py`
- Modify: `backend/src/porto_chatbot/memory/persist.py`
- Test: `backend/tests/memory/test_conversation_memory.py`
- Test: `backend/tests/test_memory_compaction.py`

**Interfaces:**
- Consumes: repository contract from Task 1.
- Produces: `SessionStore.get_messages_by_ids(ids: Sequence[str]) -> dict[str, MessageRecord]`; `ConversationMemory` accepts a repository and collection spec while retaining `index/search/count/reset` compatibility.

- [ ] **Step 1: Write failing tests for hydration, metadata, and mismatch behavior**

Assert search hydrates text by message ID, preserves hit order, omits missing SQLite rows, and filters by `session_id`. Assert a typed mismatch returns no Memory hits and an index mismatch leaves the SQLite message present without a receipt/vector deletion.

- [ ] **Step 2: Run Memory tests and confirm failures**

Run: `cd backend && uv run pytest tests/memory/test_conversation_memory.py tests/test_memory_compaction.py -q`
Expected: FAIL because hydration and repository injection do not exist.

- [ ] **Step 3: Add `get_messages_by_ids` using one parameterized SQLite query**

Return a dictionary keyed by message ID, handle an empty list without SQL, and preserve complete `MessageRecord` values.

- [ ] **Step 4: Replace direct Chroma calls in `ConversationMemory`**

Store compatibility metadata in `CollectionSpec`; catch only `DimensionMismatchError` at the service boundary; log and degrade without calling `reset`. Convert repository scores directly to `SourceChunk`.

- [ ] **Step 5: Run Memory tests and Ruff**

Run: `cd backend && uv run pytest tests/memory tests/test_memory_compaction.py -q && uv run ruff check src/porto_chatbot/memory`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/src/porto_chatbot/memory backend/tests/memory backend/tests/test_memory_compaction.py
git commit -m "refactor(memory): use vector repository with SQLite hydration"
```

### Task 4: Add Compatibility Factory and Replace Construction Points

**Files:**
- Create: `backend/src/porto_chatbot/vector_stores/factory.py`
- Modify: `backend/src/porto_chatbot/api/deps.py`
- Modify: `backend/src/porto_chatbot/agent_sdk/backend.py`
- Modify: `backend/src/porto_chatbot/workflow_executor.py`
- Modify: `backend/src/porto_chatbot/agent/orchestrator.py`
- Test: `backend/tests/test_agent_backend_injection.py`
- Test: `backend/tests/test_agent_sdk_backend.py`
- Test: `backend/tests/test_workflow_executor.py`

**Interfaces:**
- Produces: `VectorRepositoryFactory.get(settings)`, `close_all()`, and test-only `clear()`; compatibility cache key is resolved `data_dir`.

- [ ] **Step 1: Add failing tests proving every runtime path uses one factory**

Monkeypatch the factory with a fake and assert API, Agent SDK, workflow, and orchestrator paths do not instantiate `chromadb.PersistentClient` or `ConversationMemory(settings)` directly.

- [ ] **Step 2: Run focused dependency-injection tests**

Run: `cd backend && uv run pytest tests/test_agent_backend_injection.py tests/test_agent_sdk_backend.py tests/test_workflow_executor.py -q`
Expected: FAIL on direct constructors.

- [ ] **Step 3: Implement the data-dir keyed factory and close lifecycle**

Build Chroma repositories from effective settings, inject them into KB/Memory services, and clear cached repositories during app shutdown and tests.

- [ ] **Step 4: Replace all direct construction points**

Search with `rg 'PersistentClient|LocalVectorStore\(|ConversationMemory\(' backend/src/porto_chatbot` and leave direct Chroma construction only inside `vector_stores/chroma.py`.

- [ ] **Step 5: Run injection tests and Ruff**

Run: `cd backend && uv run pytest tests/test_agent_backend_injection.py tests/test_agent_sdk_backend.py tests/test_workflow_executor.py tests/test_api.py -q && uv run ruff check src/porto_chatbot`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/src/porto_chatbot backend/tests
git commit -m "refactor(runtime): centralize vector repository construction"
```

### Task 5: Add Immutable KB and BM25 Generations

**Files:**
- Create: `backend/src/porto_chatbot/kb_generation_store.py`
- Modify: `backend/src/porto_chatbot/index_supervisor.py`
- Modify: `backend/src/porto_chatbot/bm25_index.py`
- Modify: `backend/src/porto_chatbot/vector_store.py`
- Create: `backend/tests/test_kb_generations.py`
- Modify: `backend/tests/test_bm25_index.py`

**Interfaces:**
- Produces: `KbGeneration(id, collection_name, bm25_path, status)`, `KbGenerationStore.allocate()`, `activate()`, `current()`; pre-profile physical name `porto_kb__g<N>`.

- [ ] **Step 1: Write failing generation tests**

Assert generation 1 bootstraps the legacy collection/path, a rebuild writes generation 2 without deleting generation 1, a failed build leaves generation 1 active, successful activation switches collection and BM25 cache together, and cache keys include generation.

- [ ] **Step 2: Run generation tests**

Run: `cd backend && uv run pytest tests/test_kb_generations.py tests/test_bm25_index.py -q`
Expected: FAIL because generation storage does not exist.

- [ ] **Step 3: Implement SQLite generation state with WAL and busy timeout**

Use an explicit transaction in `activate`; store resolved collection and BM25 paths rather than deriving them. Bootstrap existing unsuffixed assets as generation 1.

- [ ] **Step 4: Make `IndexSupervisor` prepare and activate immutable generations**

Build into the allocated collection and `bm25s_index/g<N>`, verify counts, then activate. Mark failed generations without changing the current pointer.

- [ ] **Step 5: Run generation, supervisor, and vector tests**

Run: `cd backend && uv run pytest tests/test_kb_generations.py tests/test_bm25_index.py tests/test_vector_store.py tests/test_health_embedding.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/src/porto_chatbot/kb_generation_store.py backend/src/porto_chatbot/index_supervisor.py backend/src/porto_chatbot/bm25_index.py backend/src/porto_chatbot/vector_store.py backend/tests
git commit -m "feat(rag): add immutable KB and BM25 generations"
```

### Task 6: Foundation Verification

**Files:**
- Modify only files required to resolve failures caused by Tasks 1-5.

- [ ] **Step 1: Run the complete backend suite**

Run: `cd backend && uv run pytest -q`
Expected: PASS, excluding tests already marked `integration` by project configuration.

- [ ] **Step 2: Run backend lint**

Run: `cd backend && uv run ruff check src tests`
Expected: PASS.

- [ ] **Step 3: Run frontend compatibility checks**

Run: `cd frontend && npm run lint && npm run build`
Expected: PASS; no API type break reaches the existing frontend.

- [ ] **Step 4: Verify Chroma remains the only concrete dependency site**

Run: `rg 'import chromadb|PersistentClient' backend/src/porto_chatbot`
Expected: matches only `vector_stores/chroma.py`.

- [ ] **Step 5: Commit verification fixes, if any**

```bash
git add backend/src backend/tests frontend/src
git commit -m "test(vector-store): verify storage foundation"
```
