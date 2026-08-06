# RAG 检索优化配置化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 HyDE / Multi-Query / Decomposition / Step-Back 查询变换和 Off/Binary/Adaptive 路由做成 chat 与 workflow 各自可选的配置项，全链路（后端算法 → 配置 → 前端 UI）支持。

**Architecture:** 两个正交维度——`intent_routing_mode`（chat 专有，决定走哪条路）和 `query_transform_strategy`（chat/workflow 各自，决定检索前怎么改写）。新建 `query_transform.py` 封装"transform+检索"完整编排；`vector_store` 拆出 `_search_raw` 供编排复用；新增 `rag_chat`/`rag_workflow` 两个 config_store namespace 实现场景分离。LLM 为硬依赖，偶发失败 fail-open + `transform_degraded` 可见降级。

**Tech Stack:** Python 3.12 + FastAPI + pydantic-settings + llama-index（检索编排）+ LangGraph；Next.js + React + TypeScript（前端）。

**Spec:** `docs/superpowers/specs/2026-08-07-rag-optimization-design.md`

## Global Constraints

- **Python 3.12 + uv**：后端用 `cd backend && uv run pytest`；代码风格 `from __future__ import annotations` + ruff。
- **默认值零行为变化**：所有新字段默认值 = 现状行为（chat: `binary`+`none`；workflow: `none`）。每个 task 完成后现有测试必须全过。
- **降级哲学**：LLM 为硬依赖（无 key 不存在运行场景）。偶发失败 fail-open 回退 `_search_raw` + `degraded=True` + 日志。
- **卡片标题用英文技术名**（`Off`/`Binary`/`Adaptive`/`None`/`HyDE`/`Multi-Query`/`Decomposition`/`Step-Back`），介绍用中文。
- **前端无单测**：前端验证用 `cd frontend && npm run build`（参考 [[rtk-wrapper-unreliable-typecheck]]，tsc/eslint 假阳性，以 build 为准）。
- **Next.js 警告**：`frontend/AGENTS.md` 标注此 Next.js 有 breaking changes，写前端代码前读 `node_modules/next/dist/docs/` 相关 guide。
- **Commit**：每个 task 末尾 commit；commit message 用 `feat:`/`refactor:`/`test:` 前缀；当前若在 main 先开 `feat/rag-query-optimization` 分支。
- **不碰**：embedding LOCAL、spec 模板拼接等现有降级路径（超出范围）；现有 `rag` namespace（基础检索配置）不动。

---

## File Structure

**新建：**
- `backend/src/porto_chatbot/query_transform.py` — transform+检索编排核心（TransformResult + retrieve_with_transform + 各策略 + RRF 工具）
- `backend/tests/test_query_transform.py` — query_transform 单测

**修改（后端）：**
- `backend/src/porto_chatbot/models/enums.py` — 新增 IntentRoutingMode / QueryTransformStrategy，ChatIntent 扩展
- `backend/src/porto_chatbot/settings.py` — 新字段（场景分离配置）
- `backend/src/porto_chatbot/config_store.py` — RAG_CHAT/RAG_WORKFLOW 两 namespace
- `backend/src/porto_chatbot/models/payload.py` — RagChat/RagWorkflowSettingsPayload + AppSettings 扩展
- `backend/src/porto_chatbot/models/__init__.py` — 导出新 payload
- `backend/src/porto_chatbot/api/deps.py` — default/effective_rag_chat/workflow_settings + apply 扩展
- `backend/src/porto_chatbot/api/routes/settings.py` — GET/PUT 处理新子对象
- `backend/src/porto_chatbot/vector_store.py` — 拆 `_search_raw`
- `backend/src/porto_chatbot/intent.py` — routing_mode 参数 + adaptive 三级
- `backend/src/porto_chatbot/models/chat.py` — ChatResponse 加 transform_degraded
- `backend/src/porto_chatbot/agent/langchain_chat.py` — 接入 routing + transform
- `backend/src/porto_chatbot/agent/nodes/retrieve.py` — 接入 transform

**修改（前端）：**
- `frontend/src/lib/types.ts` — AppSettings 扩展 rag_chat/rag_workflow
- `frontend/src/lib/api.ts` — 透传
- `frontend/src/components/porto-workbench.tsx` — 新 retrieval_optimization tab + StrategyCardGroup + 两表单

**修改（测试）：**
- `backend/tests/test_intent.py`、`test_config_store.py`、`test_settings_fields.py`、`test_settings_api.py`

---

## Task 依赖

```
Task1(枚举+settings) ──┬─→ Task2(config_store) ─→ Task3(payload+api)
                       ├─→ Task7(intent 扩展)
Task4(vector_store _search_raw) ─→ Task5(query_transform 核心) ─→ Task6(策略)
Task5,Task6,Task7 ─→ Task8(接入点)
Task3 ─→ Task9(前端)
```

---

### Task 1: 后端枚举 + settings 字段

**Files:**
- Modify: `backend/src/porto_chatbot/models/enums.py`
- Modify: `backend/src/porto_chatbot/settings.py`
- Test: `backend/tests/test_settings_fields.py`

**Interfaces:**
- Produces: `IntentRoutingMode`, `QueryTransformStrategy`（enums）；`Settings.chat_intent_routing_mode`/`chat_query_transform_strategy`/`workflow_query_transform_strategy`/`multi_query_count`/`hyde_fallback_threshold`；`ChatIntent.QUICK_RAG`/`DEEP_RAG`

- [ ] **Step 1: 在 enums.py 加新枚举 + 扩展 ChatIntent**

在 `models/enums.py` 的 `RetrievalMethod` 后面加：

```python
# ── 查询变换策略（检索前如何改写 query）──
class QueryTransformStrategy(StrEnum):
    NONE = "none"
    HYDE = "hyde"
    MULTI_QUERY = "multi_query"
    DECOMPOSITION = "decomposition"
    STEP_BACK = "step_back"


# ── 意图路由模式（chat 专有）──
class IntentRoutingMode(StrEnum):
    OFF = "off"
    BINARY = "binary"
    ADAPTIVE = "adaptive"
```

把 `ChatIntent` 扩展（替换现有两成员定义）：

```python
class ChatIntent(StrEnum):
    DIRECT = "direct"
    RAG = "rag"               # binary 模式
    QUICK_RAG = "quick_rag"   # adaptive：快速检索（强制 none）
    DEEP_RAG = "deep_rag"     # adaptive：深度检索（套 transform）
```

- [ ] **Step 2: 在 settings.py 加导入 + 新字段**

`settings.py` 顶部 import 加 `IntentRoutingMode`, `QueryTransformStrategy`。在 `rerank_choice_batch_size` 字段后（`@field_validator` 之前）加：

```python
    # --- RAG 检索优化（场景分离）---
    chat_intent_routing_mode: IntentRoutingMode = IntentRoutingMode.BINARY
    chat_query_transform_strategy: QueryTransformStrategy = QueryTransformStrategy.NONE
    workflow_query_transform_strategy: QueryTransformStrategy = QueryTransformStrategy.NONE
    multi_query_count: int = Field(default=4, ge=2, le=8)
    hyde_fallback_threshold: float = Field(default=0.3, ge=0.0, le=1.0)  # 占位，本次不实现逻辑
```

- [ ] **Step 3: 写测试验证默认值**

在 `test_settings_fields.py` 末尾加：

```python
def test_rag_optimization_defaults():
    s = Settings()
    assert s.chat_intent_routing_mode == "binary"
    assert s.chat_query_transform_strategy == "none"
    assert s.workflow_query_transform_strategy == "none"
    assert s.multi_query_count == 4
```

- [ ] **Step 4: 运行测试**

Run: `cd backend && uv run pytest tests/test_settings_fields.py -v`
Expected: PASS（含新测试）

- [ ] **Step 5: 跑全量确保无回归**

Run: `cd backend && uv run pytest -q`
Expected: 全过（默认值零行为变化）

- [ ] **Step 6: Commit**

```bash
git add backend/src/porto_chatbot/models/enums.py backend/src/porto_chatbot/settings.py backend/tests/test_settings_fields.py
git commit -m "feat: add QueryTransformStrategy/IntentRoutingMode enums + settings fields"
```

---

### Task 2: config_store 两 namespace

**Files:**
- Modify: `backend/src/porto_chatbot/config_store.py`
- Modify: `backend/src/porto_chatbot/models/payload.py`（仅加两个空 payload 骨架，Task 3 补字段）
- Modify: `backend/src/porto_chatbot/models/__init__.py`
- Test: `backend/tests/test_config_store.py`

**Interfaces:**
- Consumes: Task 1 的枚举
- Produces: `ConfigStore.get_rag_chat_settings()`/`save_rag_chat_settings()`、`get_rag_workflow_settings()`/`save_rag_workflow_settings()`；`RAG_CHAT_SETTING_KEYS`/`RAG_WORKFLOW_SETTING_KEYS`

- [ ] **Step 1: 在 payload.py 加两个 payload 骨架**

在 `DocumentSettingsPayload` 后、`AppSettingsPayload` 前加：

```python
class RagChatSettingsPayload(BaseModel):
    intent_routing_mode: IntentRoutingMode | None = None
    query_transform_strategy: QueryTransformStrategy | None = None
    multi_query_count: int | None = Field(default=None, ge=2, le=8)
    hyde_fallback_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class RagWorkflowSettingsPayload(BaseModel):
    query_transform_strategy: QueryTransformStrategy | None = None
    multi_query_count: int | None = Field(default=None, ge=2, le=8)
```

payload.py 顶部 import 加 `IntentRoutingMode`, `QueryTransformStrategy`。

`models/__init__.py`：照现有 `RagSettingsPayload` 的导入/导出模式，加 `RagChatSettingsPayload`, `RagWorkflowSettingsPayload`。

- [ ] **Step 2: 在 config_store.py 加两 namespace**

import 加 `RagChatSettingsPayload`, `RagWorkflowSettingsPayload`。在 `DOCUMENT_SETTING_KEYS` 后加：

```python
RAG_CHAT_SETTING_KEYS = {
    "intent_routing_mode",
    "query_transform_strategy",
    "multi_query_count",
    "hyde_fallback_threshold",
}

RAG_WORKFLOW_SETTING_KEYS = {
    "query_transform_strategy",
    "multi_query_count",
}
```

在 `ConfigStore` 类里（`save_document_settings` 后）照 `get_rag_settings`/`save_rag_settings` 同模式加四个方法：

```python
    def get_rag_chat_settings(self) -> RagChatSettingsPayload:
        return RagChatSettingsPayload(**self._get_namespace("rag_chat", RAG_CHAT_SETTING_KEYS))

    def save_rag_chat_settings(self, payload: RagChatSettingsPayload) -> RagChatSettingsPayload:
        current = self.get_rag_chat_settings().model_dump(exclude_none=True)
        current.update(payload.model_dump(exclude_none=True))
        saved = RagChatSettingsPayload(**current)
        self._save_namespace("rag_chat", saved.model_dump(exclude_none=True))
        return saved

    def get_rag_workflow_settings(self) -> RagWorkflowSettingsPayload:
        return RagWorkflowSettingsPayload(**self._get_namespace("rag_workflow", RAG_WORKFLOW_SETTING_KEYS))

    def save_rag_workflow_settings(self, payload: RagWorkflowSettingsPayload) -> RagWorkflowSettingsPayload:
        current = self.get_rag_workflow_settings().model_dump(exclude_none=True)
        current.update(payload.model_dump(exclude_none=True))
        saved = RagWorkflowSettingsPayload(**current)
        self._save_namespace("rag_workflow", saved.model_dump(exclude_none=True))
        return saved
```

- [ ] **Step 3: 写测试**

在 `test_config_store.py` 加（参照现有 rag namespace 测试的 tmp_path 用法）：

```python
def test_rag_chat_settings_roundtrip(tmp_path):
    from porto_chatbot.config_store import ConfigStore
    from porto_chatbot.settings import Settings
    store = ConfigStore(Settings(data_dir=tmp_path / "d", log_dir=tmp_path / "l", kb_dirs=[tmp_path / "kb"]))
    assert store.get_rag_chat_settings().intent_routing_mode is None  # 空库
    saved = store.save_rag_chat_settings(RagChatSettingsPayload(intent_routing_mode="adaptive", query_transform_strategy="hyde"))
    assert saved.intent_routing_mode == "adaptive"
    assert saved.query_transform_strategy == "hyde"
    # 重新读
    assert store.get_rag_chat_settings().intent_routing_mode == "adaptive"


def test_rag_workflow_settings_roundtrip(tmp_path):
    from porto_chatbot.config_store import ConfigStore
    from porto_chatbot.settings import Settings
    store = ConfigStore(Settings(data_dir=tmp_path / "d", log_dir=tmp_path / "l", kb_dirs=[tmp_path / "kb"]))
    saved = store.save_rag_workflow_settings(RagWorkflowSettingsPayload(query_transform_strategy="multi_query", multi_query_count=5))
    assert saved.query_transform_strategy == "multi_query"
    assert store.get_rag_workflow_settings().multi_query_count == 5
```

import `RagChatSettingsPayload`, `RagWorkflowSettingsPayload` from `porto_chatbot.models`.

- [ ] **Step 4: 运行测试 + 全量回归**

Run: `cd backend && uv run pytest tests/test_config_store.py -v && uv run pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/config_store.py backend/src/porto_chatbot/models/payload.py backend/src/porto_chatbot/models/__init__.py backend/tests/test_config_store.py
git commit -m "feat: add rag_chat/rag_workflow config_store namespaces"
```

---

### Task 3: payload AppSettings 扩展 + API effective/deps/routes

**Files:**
- Modify: `backend/src/porto_chatbot/models/payload.py`（AppSettingsPayload/Response 加字段）
- Modify: `backend/src/porto_chatbot/api/deps.py`
- Modify: `backend/src/porto_chatbot/api/routes/settings.py`
- Test: `backend/tests/test_settings_api.py`

**Interfaces:**
- Consumes: Task 2 的 payload + ConfigStore 方法
- Produces: `effective_rag_chat_settings()`/`effective_rag_workflow_settings()`；`AppSettingsResponse.rag_chat`/`rag_workflow`

- [ ] **Step 1: 扩展 AppSettingsPayload / AppSettingsResponse**

`payload.py`：

```python
class AppSettingsPayload(BaseModel):
    rag: RagSettingsPayload | None = None
    agent: AgentSettingsPayload | None = None
    document: DocumentSettingsPayload | None = None
    rag_chat: RagChatSettingsPayload | None = None
    rag_workflow: RagWorkflowSettingsPayload | None = None


class AppSettingsResponse(BaseModel):
    rag: RagSettingsPayload
    agent: AgentSettingsPayload
    document: DocumentSettingsPayload
    rag_chat: RagChatSettingsPayload
    rag_workflow: RagWorkflowSettingsPayload
```

- [ ] **Step 2: deps.py 加 default + effective 函数**

import 加 `RagChatSettingsPayload`, `RagWorkflowSettingsPayload`。在 `effective_document_settings` 后加：

```python
def default_rag_chat_settings() -> RagChatSettingsPayload:
    s = current_settings()
    return RagChatSettingsPayload(
        intent_routing_mode=s.chat_intent_routing_mode,
        query_transform_strategy=s.chat_query_transform_strategy,
        multi_query_count=s.multi_query_count,
        hyde_fallback_threshold=s.hyde_fallback_threshold,
    )


def default_rag_workflow_settings() -> RagWorkflowSettingsPayload:
    s = current_settings()
    return RagWorkflowSettingsPayload(
        query_transform_strategy=s.workflow_query_transform_strategy,
        multi_query_count=s.multi_query_count,
    )


def effective_rag_chat_settings(payload: RagChatSettingsPayload | None = None) -> RagChatSettingsPayload:
    updates = default_rag_chat_settings().model_dump(exclude_none=True)
    updates.update(get_config_store().get_rag_chat_settings().model_dump(exclude_none=True))
    if payload:
        updates.update(payload.model_dump(exclude_none=True))
    return RagChatSettingsPayload(**updates)


def effective_rag_workflow_settings(payload: RagWorkflowSettingsPayload | None = None) -> RagWorkflowSettingsPayload:
    updates = default_rag_workflow_settings().model_dump(exclude_none=True)
    updates.update(get_config_store().get_rag_workflow_settings().model_dump(exclude_none=True))
    if payload:
        updates.update(payload.model_dump(exclude_none=True))
    return RagWorkflowSettingsPayload(**updates)
```

- [ ] **Step 3: routes/settings.py 处理新子对象**

import 加 `effective_rag_chat_settings`, `effective_rag_workflow_settings`。改 GET/PUT：

```python
@router.get("/api/settings", response_model=AppSettingsResponse)
def get_app_settings():
    logger.info("settings read")
    return AppSettingsResponse(
        rag=effective_rag_settings(),
        agent=effective_agent_settings(),
        document=effective_document_settings(),
        rag_chat=effective_rag_chat_settings(),
        rag_workflow=effective_rag_workflow_settings(),
    )


@router.put("/api/settings", response_model=AppSettingsResponse)
def save_app_settings(req: AppSettingsPayload):
    store = get_config_store()
    if req.rag:
        store.save_rag_settings(req.rag)
    if req.agent:
        store.save_agent_settings(req.agent)
    if req.document:
        store.save_document_settings(req.document)
    if req.rag_chat:
        logger.info("settings save namespace=rag_chat")
        store.save_rag_chat_settings(req.rag_chat)
    if req.rag_workflow:
        logger.info("settings save namespace=rag_workflow")
        store.save_rag_workflow_settings(req.rag_workflow)
    return get_app_settings()
```

- [ ] **Step 4: 写 API 测试**

在 `test_settings_api.py` 加（参照现有 GET/PUT 测试的 client 用法）：

```python
def test_settings_include_rag_optimization(client):
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["rag_chat"]["intent_routing_mode"] == "binary"  # 默认值
    assert body["rag_chat"]["query_transform_strategy"] == "none"
    assert body["rag_workflow"]["query_transform_strategy"] == "none"


def test_settings_save_rag_chat(client):
    resp = client.put("/api/settings", json={
        "rag_chat": {"intent_routing_mode": "adaptive", "query_transform_strategy": "hyde"}
    })
    assert resp.status_code == 200
    assert resp.json()["rag_chat"]["intent_routing_mode"] == "adaptive"
    # 持久化
    assert client.get("/api/settings").json()["rag_chat"]["query_transform_strategy"] == "hyde"
```

- [ ] **Step 5: 运行测试 + 全量回归**

Run: `cd backend && uv run pytest tests/test_settings_api.py -v && uv run pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/src/porto_chatbot/models/payload.py backend/src/porto_chatbot/api/deps.py backend/src/porto_chatbot/api/routes/settings.py backend/tests/test_settings_api.py
git commit -m "feat: expose rag_chat/rag_workflow via settings API"
```

---

### Task 4: vector_store 拆 _search_raw

**Files:**
- Modify: `backend/src/porto_chatbot/vector_store.py:187-215`（search 方法）
- Test: `backend/tests/test_vector_store.py`

**Interfaces:**
- Produces: `LocalVectorStore._search_raw(query, top_k) -> list[SourceChunk]`（不含 rerank）；`search()` 改为调 `_search_raw` + rerank

- [ ] **Step 1: 写测试验证 _search_raw 等价于关闭 rerank 的 search**

在 `test_vector_store.py` 加（参照现有 search 测试的 store 构造方式）：

```python
def test_search_raw_no_rerank(store_with_docs):
    store = store_with_docs
    store.settings.rerank_enabled = False
    raw = store._search_raw("查询", top_k=3)
    full = store.search("查询", top_k=3)
    # rerank 关闭时两者一致
    assert [r.id for r in raw] == [r.id for r in full]
    assert len(raw) <= 3
```

（`store_with_docs` 若不存在，参照 `test_vector_store.py` 现有构造带文档的 store 的 fixture/辅助函数。）

- [ ] **Step 2: 重构 search → _search_raw + search**

把 `vector_store.py` 现有 `search` 方法（line 187 起）的"embed + method 分发"那段抽到 `_search_raw`：

```python
    def _search_raw(self, query: str, top_k: int) -> list[SourceChunk]:
        """基础检索（vector/bm25/hybrid），不含 rerank。供 query_transform 编排复用。"""
        collection = self._compatible_collection()
        if not self._is_collection_compatible(collection) or self._safe_count(collection) == 0:
            self.logger.info("search_raw skipped index unavailable query_chars=%s", len(query))
            return []
        query_embedding = self.embeddings.embed_query(query)
        stored_dimensions = self._collection_embedding_dimensions(collection)
        if stored_dimensions is not None and stored_dimensions != len(query_embedding):
            self.logger.warning(
                "embedding dimension mismatch detected stored=%s current=%s action=skip",
                stored_dimensions, len(query_embedding),
            )
            return []
        method = self.settings.retrieval_method
        if method == RetrievalMethod.VECTOR:
            return self._vector_search(collection, query_embedding, top_k)
        if method == RetrievalMethod.BM25:
            return self._bm25_search(collection, query, top_k)
        return self._hybrid_search(collection, query_embedding, query, top_k)

    def search(self, query: str, top_k: int | None = None) -> list[SourceChunk]:
        resolved_top_k = top_k or self.settings.top_k
        self.logger.info("search start query_chars=%s top_k=%s method=%s", len(query), resolved_top_k, self.settings.retrieval_method)
        rows = self._search_raw(query, resolved_top_k)
        if self.settings.rerank_enabled and rows:
            rows = rerank_chunks(rows, query, self.settings)
        self.logger.info("search finish results=%s", len(rows))
        return rows
```

- [ ] **Step 3: 运行测试 + 全量回归**

Run: `cd backend && uv run pytest tests/test_vector_store.py -v && uv run pytest -q`
Expected: PASS（search 行为不变，只是内部拆分）

- [ ] **Step 4: Commit**

```bash
git add backend/src/porto_chatbot/vector_store.py backend/tests/test_vector_store.py
git commit -m "refactor: extract _search_raw from search for transform orchestration"
```

---

### Task 5: query_transform.py 核心（TransformResult + none + 降级 + RRF 工具）

**Files:**
- Create: `backend/src/porto_chatbot/query_transform.py`
- Test: `backend/tests/test_query_transform.py`

**Interfaces:**
- Consumes: Task 4 的 `store._search_raw`；`rerank_chunks`（retrieval.py）；`LLMClient`
- Produces: `TransformResult`（dataclass: chunks/degraded/degrade_reason）；`retrieve_with_transform(query, strategy, store, settings, llm, top_k) -> TransformResult`；`_rrf_fuse`/`_merge_dedupe`（内部）

- [ ] **Step 1: 写测试（none 等价 + 降级）**

创建 `tests/test_query_transform.py`：

```python
from porto_chatbot.query_transform import retrieve_with_transform, TransformResult
from porto_chatbot.models.enums import QueryTransformStrategy


def test_none_strategy_equals_store_search(store_with_docs):
    store = store_with_docs
    expected = store.search("查询", top_k=3)
    result = retrieve_with_transform(
        "查询", QueryTransformStrategy.NONE, store, store.settings, llm=None, top_k=3
    )
    assert isinstance(result, TransformResult)
    assert result.degraded is False
    assert [r.id for r in result.chunks] == [r.id for r in expected]


def test_hyde_fallback_on_llm_failure(store_with_docs, monkeypatch):
    """LLM 不可用/抛异常 → fail-open 回退 _search_raw + degraded=True。"""
    store = store_with_docs

    class BoomLLM:
        enabled = True
        def complete(self, *a, **k):
            raise RuntimeError("timeout")

    result = retrieve_with_transform(
        "查询", QueryTransformStrategy.HYDE, store, store.settings, llm=BoomLLM(), top_k=3
    )
    assert result.degraded is True
    assert "llm_call_failed" in result.degrade_reason
    # 仍返回基础检索结果，不崩
    assert isinstance(result.chunks, list)
```

（`store_with_docs` fixture 若 test_vector_store.py 已定义，提取到 conftest.py 共享；否则在本文件内参照构造。）

- [ ] **Step 2: 创建 query_transform.py 核心**

```python
from __future__ import annotations

from dataclasses import dataclass, field

from .logging_utils import get_component_logger
from .models import SourceChunk
from .models.enums import QueryTransformStrategy
from .retrieval import rerank_chunks
from .settings import Settings

logger = get_component_logger("query_transform")


@dataclass
class TransformResult:
    chunks: list[SourceChunk]
    degraded: bool = False
    degrade_reason: str = ""


def _rrf_fuse(rankings: list[list[SourceChunk]], k: int = 60) -> list[SourceChunk]:
    """Reciprocal Rank Fusion：多组 ranking 按 1/(rank+k) 求和合并。"""
    scores: dict[str, float] = {}
    best: dict[str, SourceChunk] = {}
    for ranking in rankings:
        for rank, chunk in enumerate(ranking):
            scores[chunk.id] = scores.get(chunk.id, 0.0) + 1.0 / (rank + k)
            best.setdefault(chunk.id, chunk)
    ordered = sorted(scores, key=lambda cid: scores[cid], reverse=True)
    return [{**best[cid], "score": round(scores[cid], 4)} for cid in ordered]  # type: ignore[misc]


def _merge_dedupe(rankings: list[list[SourceChunk]]) -> list[SourceChunk]:
    """子问题结果按出现顺序合并去重（首个出现的位置保留）。"""
    seen: set[str] = set()
    out: list[SourceChunk] = []
    for ranking in rankings:
        for chunk in ranking:
            if chunk.id not in seen:
                seen.add(chunk.id)
                out.append(chunk)
    return out


def retrieve_with_transform(
    query: str,
    strategy: QueryTransformStrategy,
    store,
    settings: Settings,
    llm,
    top_k: int,
) -> TransformResult:
    """按策略 transform + 检索。LLM 失败 fail-open 回退 _search_raw + degraded。"""
    if strategy == QueryTransformStrategy.NONE:
        return TransformResult(store.search(query, top_k))

    try:
        if strategy == QueryTransformStrategy.HYDE:
            fake_doc = _generate_hypothetical(llm, query)
            rows = store._search_raw(fake_doc, top_k)
        elif strategy == QueryTransformStrategy.MULTI_QUERY:
            variants = _generate_query_variants(llm, query, settings.multi_query_count)
            rows = _rrf_fuse([store._search_raw(v, top_k) for v in variants])
        elif strategy == QueryTransformStrategy.DECOMPOSITION:
            sub_qs = _decompose(llm, query)
            rows = _merge_dedupe([store._search_raw(s, top_k) for s in sub_qs])
        elif strategy == QueryTransformStrategy.STEP_BACK:
            abstract = _step_back(llm, query)
            rows = store._search_raw(abstract, top_k)
        else:
            rows = store._search_raw(query, top_k)
    except Exception:
        logger.exception("transform failed strategy=%s → fallback raw", strategy)
        rows = store._search_raw(query, top_k)
        return TransformResult(rows, degraded=True, degrade_reason="llm_call_failed")

    if settings.rerank_enabled:
        rows = rerank_chunks(rows, query, settings)
    return TransformResult(rows)


# --- 各策略 LLM 生成函数（Task 6 实现）---
def _generate_hypothetical(llm, query: str) -> str:
    raise NotImplementedError  # Task 6

def _generate_query_variants(llm, query: str, n: int) -> list[str]:
    raise NotImplementedError  # Task 6

def _decompose(llm, query: str) -> list[str]:
    raise NotImplementedError  # Task 6

def _step_back(llm, query: str) -> str:
    raise NotImplementedError  # Task 6
```

> 注：`_rrf_fuse` 用 `{**best[cid], "score": ...}` 重建 SourceChunk——若 SourceChunk 是 pydantic 模型，改为 `best[cid].model_copy(update={"score": ...})`。实现时按 `models/common.py` 的 SourceChunk 定义调整。

- [ ] **Step 3: 运行测试**

Run: `cd backend && uv run pytest tests/test_query_transform.py -v`
Expected: `test_none_strategy_equals_store_search` PASS；`test_hyde_fallback_on_llm_failure` PASS（NotImplementedError 被 try/except 捕获 → degraded）。

- [ ] **Step 4: 全量回归**

Run: `cd backend && uv run pytest -q`
Expected: 全过

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/query_transform.py backend/tests/test_query_transform.py
git commit -m "feat: query_transform core (TransformResult + none + fail-open + RRF)"
```

---

### Task 6: query_transform 各策略实现

**Files:**
- Modify: `backend/src/porto_chatbot/query_transform.py`（替换 4 个 NotImplementedError 函数）
- Test: `backend/tests/test_query_transform.py`

**Interfaces:**
- Consumes: `LLMClient.complete`（生成假答案/改写/拆分）
- Produces: 4 个策略函数的真实实现

- [ ] **Step 1: 写各策略测试（mock LLM.complete）**

在 `test_query_transform.py` 加：

```python
def test_hyde_uses_fake_doc_for_search(store_with_docs, monkeypatch):
    store = store_with_docs
    captured = {}

    class FakeLLM:
        enabled = True
        def complete(self, system, user):
            captured["called"] = True
            return "这是一个假设性的答案文档，描述了支付风控的架构。"

    monkeypatch.setattr(store, "_search_raw", lambda q, top_k: captured.setdefault("query_used", q) or [])
    result = retrieve_with_transform("支付风控", QueryTransformStrategy.HYDE, store, store.settings, FakeLLM(), top_k=3)
    assert result.degraded is False
    assert "假设性" in captured["query_used"]  # 用假答案而非原 query 检索


def test_multi_query_fuses_variants(store_with_docs, monkeypatch):
    store = store_with_docs
    calls = []

    class FakeLLM:
        enabled = True
        def complete(self, system, user):
            return "改写1\n改写2\n改写3"

    monkeypatch.setattr(store, "_search_raw", lambda q, top_k: calls.append(q) or [])
    retrieve_with_transform("查询", QueryTransformStrategy.MULTI_QUERY, store, store.settings, FakeLLM(), top_k=3)
    assert len(calls) == 3  # 3 个改写各检索一次


def test_decomposition_splits_and_merges(store_with_docs, monkeypatch):
    store = store_with_docs
    calls = []

    class FakeLLM:
        enabled = True
        def complete(self, system, user):
            return "子问题1\n子问题2"

    monkeypatch.setattr(store, "_search_raw", lambda q, top_k: calls.append(q) or [])
    retrieve_with_transform("复杂问题", QueryTransformStrategy.DECOMPOSITION, store, store.settings, FakeLLM(), top_k=3)
    assert len(calls) == 2
```

- [ ] **Step 2: 实现 4 个策略函数**

替换 query_transform.py 末尾的 4 个 `raise NotImplementedError`：

```python
def _generate_hypothetical(llm, query: str) -> str:
    """HyDE：让 LLM 生成一个假设性答案文档，用它（而非原问题）去检索。"""
    return llm.complete(
        "请根据用户问题，写一段假设性的、像知识库文档一样的答案（中文，3-5 句）。"
        "只输出答案正文，不要解释、不要说'假设'。",
        f"用户问题: {query}",
    ).strip()


def _generate_query_variants(llm, query: str, n: int) -> list[str]:
    """Multi-Query：生成 n 个改写 query，每行一个。"""
    raw = llm.complete(
        f"请把下面的问题改写成 {n} 个语义相同但表述不同的检索用查询（每行一个，不要编号）：",
        f"原问题: {query}",
    )
    variants = [line.strip() for line in raw.splitlines() if line.strip()]
    return variants[:n] if variants else [query]


def _decompose(llm, query: str) -> list[str]:
    """Decomposition：把复杂问题拆成子问题，每行一个。"""
    raw = llm.complete(
        "请把下面的问题拆成若干个独立的子问题（每行一个，不要编号），便于分别检索：",
        f"原问题: {query}",
    )
    subs = [line.strip() for line in raw.splitlines() if line.strip()]
    return subs if subs else [query]


def _step_back(llm, query: str) -> str:
    """Step-Back：抽象出更高层的背景问题。"""
    return llm.complete(
        "请把下面的问题抽象成一个更宽泛的背景问题（一句话），用于检索相关背景知识。只输出问题本身：",
        f"原问题: {query}",
    ).strip()
```

- [ ] **Step 3: 运行测试 + 全量回归**

Run: `cd backend && uv run pytest tests/test_query_transform.py -v && uv run pytest -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add backend/src/porto_chatbot/query_transform.py backend/tests/test_query_transform.py
git commit -m "feat: implement hyde/multi_query/decomposition/step_back strategies"
```

---

### Task 7: intent.py 扩展（routing_mode + adaptive 三级）

**Files:**
- Modify: `backend/src/porto_chatbot/intent.py`
- Test: `backend/tests/test_intent.py`

**Interfaces:**
- Consumes: Task 1 的 `IntentRoutingMode`, `ChatIntent`
- Produces: `route_chat_intent(message, settings, llm, routing_mode=IntentRoutingMode.BINARY)`；adaptive 输出 direct/quick_rag/deep_rag

- [ ] **Step 1: 写测试**

在 `test_intent.py` 加：

```python
from porto_chatbot.models.enums import IntentRoutingMode, ChatIntent


def test_routing_mode_off_skips_routing():
    """off 模式语义上由调用方直接检索，route_chat_intent 仍可调用但行为=返回 RAG。"""
    d = route_chat_intent("你好", routing_mode=IntentRoutingMode.OFF)
    # off 不做 direct 分流，规则不判 greeting
    assert d.intent in (ChatIntent.RAG, ChatIntent.QUICK_RAG, ChatIntent.DEEP_RAG)


def test_adaptive_llm_classifies_three_way(tmp_path, monkeypatch):
    llm = _enabled_llm(tmp_path)
    monkeypatch.setattr(llm, "complete_structured",
                        lambda *a, **k: {"intent": "deep_rag", "reason": "复杂架构问题"})
    d = route_chat_intent("分析支付风控的完整架构", None, llm, routing_mode=IntentRoutingMode.ADAPTIVE)
    assert d.intent == "deep_rag"


def test_adaptive_rule_fallback(tmp_path):
    """LLM 不可用时规则降级也应能产出 quick_rag/deep_rag（按复杂度关键词）。"""
    s = Settings(kb_dirs=[tmp_path / "kb"], data_dir=tmp_path / "d", log_dir=tmp_path / "l")
    llm = LLMClient(s)
    assert llm.enabled is False
    d = route_chat_intent("分析支付风控架构", None, llm, routing_mode=IntentRoutingMode.ADAPTIVE)
    assert d.intent in ("quick_rag", "deep_rag")  # 含 分析/架构 → deep_rag
```

- [ ] **Step 2: 改 intent.py**

`route_chat_intent` 加 `routing_mode` 参数；`_llm_route`/`_rule_route` 按 mode 调整枚举范围：

```python
def route_chat_intent(
    message: str,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    routing_mode: IntentRoutingMode = IntentRoutingMode.BINARY,
) -> IntentDecision:
    """意图路由。off=不分流(全 RAG)；binary=direct/rag；adaptive=direct/quick_rag/deep_rag。"""
    logger = get_component_logger("intent", settings)
    if routing_mode == IntentRoutingMode.OFF:
        return IntentDecision(ChatIntent.RAG, "routing_off")
    if llm is not None and llm.enabled:
        decision = _llm_route(message, llm, routing_mode)
        if decision is not None:
            logger.info("chat intent routed llm intent=%s reason=%s", decision.intent, decision.reason)
            return decision
    decision = _rule_route(message, routing_mode)
    logger.info("chat intent routed rule intent=%s reason=%s", decision.intent, decision.reason)
    return decision
```

`_llm_route` 改 prompt 枚举（binary 时只 direct/rag，adaptive 时 direct/quick_rag/deep_rag）：

```python
def _llm_route(message: str, llm: LLMClient, routing_mode: IntentRoutingMode) -> IntentDecision | None:
    if not message.strip():
        return None
    if routing_mode == IntentRoutingMode.ADAPTIVE:
        enums = ["direct", "quick_rag", "deep_rag"]
        desc = ("- direct：寒暄闲聊、无需查库\n"
                "- quick_rag：简单事实查询\n"
                "- deep_rag：复杂分析/架构/设计，需深度检索")
    else:
        enums = ["direct", "rag"]
        desc = ("- direct：寒暄闲聊、无需查库\n"
                "- rag：需要查询知识库")
    parsed = llm.complete_structured(
        f"你是意图分类器。判断用户消息属于：\n{desc}\n只输出 JSON。",
        f"用户消息: {message[:_MAX_INTENT_MESSAGE_CHARS]}",
        {"type": "object",
         "properties": {"intent": {"type": "string", "enum": enums}, "reason": {"type": "string"}},
         "required": ["intent", "reason"]},
    )
    if not isinstance(parsed, dict) or parsed.get("intent") not in enums:
        return None
    return IntentDecision(parsed["intent"], f"llm:{str(parsed.get('reason', ''))[:_MAX_REASON_CHARS]}")
```

`_rule_route` 加 `routing_mode`，adaptive 时含"分析/架构/设计/拆"等复杂词判 deep_rag，其余领域词判 quick_rag：

```python
_DEEP_HINTS = ("分析", "架构", "设计", "拆", "完整", "详细", "怎么实现")

def _rule_route(message: str, routing_mode: IntentRoutingMode = IntentRoutingMode.BINARY) -> IntentDecision:
    normalized = re.sub(r"\s+", " ", message).strip()
    lower = normalized.lower()
    if not normalized:
        return IntentDecision(ChatIntent.DIRECT, "empty_message")
    if GREETING_RE.match(normalized):
        return IntentDecision(ChatIntent.DIRECT, "greeting")
    if DIRECT_RE.match(normalized):
        return IntentDecision(ChatIntent.DIRECT, "smalltalk_or_help")
    if len(normalized) <= _SHORT_MESSAGE_THRESHOLD and not any(hint in lower for hint in RAG_HINTS):
        return IntentDecision(ChatIntent.DIRECT, "short_without_domain_signal")
    rag_intent = (ChatIntent.DEEP_RAG if routing_mode == IntentRoutingMode.ADAPTIVE
                  and any(h in normalized for h in _DEEP_HINTS)
                  else (ChatIntent.QUICK_RAG if routing_mode == IntentRoutingMode.ADAPTIVE else ChatIntent.RAG))
    reason = "deep_domain_request" if rag_intent == ChatIntent.DEEP_RAG else "domain_or_knowledge_request"
    return IntentDecision(rag_intent, reason)
```

import 顶部加 `from .models.enums import ChatIntent, IntentRoutingMode`（替换原仅 ChatIntent 的导入）。

- [ ] **Step 3: 运行测试 + 全量回归**

Run: `cd backend && uv run pytest tests/test_intent.py -v && uv run pytest -q`
Expected: PASS（现有 binary 测试仍过——routing_mode 默认 BINARY，行为不变）

- [ ] **Step 4: Commit**

```bash
git add backend/src/porto_chatbot/intent.py backend/tests/test_intent.py
git commit -m "feat: intent routing_mode (off/binary/adaptive) with three-way classification"
```

---

### Task 8: 接入点（langchain_chat + retrieve 节点 + transform_degraded）

**Files:**
- Modify: `backend/src/porto_chatbot/models/chat.py:30`（ChatResponse 加字段）
- Modify: `backend/src/porto_chatbot/agent/langchain_chat.py`
- Modify: `backend/src/porto_chatbot/agent/nodes/retrieve.py`
- Test: `backend/tests/test_chat_dispatch.py`（或 test_agent.py，看 chat 接入测试在哪）

**Interfaces:**
- Consumes: Task 5/6 的 `retrieve_with_transform`；Task 7 的 `route_chat_intent(routing_mode=...)`；Task 3 的 effective settings
- Produces: chat 走 routing+transform；workflow retrieve 走 transform；`ChatResponse.transform_degraded`

- [ ] **Step 1: ChatResponse 加字段**

`models/chat.py`：

```python
class ChatResponse(BaseModel):
    answer: str
    sources: list[SourceChunk]
    steps: list[AgentStep]
    evaluation: dict[str, Any] = Field(default_factory=dict)
    memory: list[SourceChunk] = Field(default_factory=list)
    transform_degraded: str | None = None  # 查询变换降级原因（None=正常）
```

- [ ] **Step 2: 改 langchain_chat.py 的 RAG 分支**

import 加：
```python
from ..models.enums import ChatIntent, IntentRoutingMode
from ..query_transform import retrieve_with_transform
```

在 `langchain_chat` 函数里，`decision = route_chat_intent(...)` 改为传 routing_mode（从 effective rag_chat settings 取）。RAG 检索段（原 `sources = store.search(req.message, top_k=top_k)`，约 line 175）改为：

```python
    rag_chat = effective_rag_chat_settings()  # 从 deps 导入
    routing_mode = rag_chat.intent_routing_mode
    transform_strategy = rag_chat.query_transform_strategy

    if routing_mode == IntentRoutingMode.OFF:
        result = retrieve_with_transform(req.message, transform_strategy, store, settings, llm, top_k)
        sources = result.chunks
        transform_degraded = result.degrade_reason if result.degraded else None
    else:
        decision = route_chat_intent(req.message, settings, llm, routing_mode=routing_mode)
        transform_degraded = None
        if decision.intent == ChatIntent.DIRECT:
            return _direct_chat_answer(req, settings, decision, llm)
        if decision.intent == ChatIntent.QUICK_RAG:
            sources = store.search(req.message, top_k=top_k)
        else:  # RAG / DEEP_RAG
            result = retrieve_with_transform(req.message, transform_strategy, store, settings, llm, top_k)
            sources = result.chunks
            transform_degraded = result.degrade_reason if result.degraded else None
```

函数返回 `ChatResponse(...)` 时加 `transform_degraded=transform_degraded`。

> 注：原代码在函数开头就调 `route_chat_intent` 做 direct 判断。本步把 routing 逻辑移到此处统一处理——off 不调 route，binary/adaptive 才调。`_direct_chat_answer` 的调用位置随之调整。仔细对照原 line 161-175 的控制流，确保 direct 分支不被遗漏。

- [ ] **Step 3: 改 retrieve 节点**

`agent/nodes/retrieve.py`：

```python
from __future__ import annotations

from ._prd import read_prd_text


def retrieve_knowledge(state, *, config):
    agent = config["configurable"]["agent"]
    agent.logger.info("step retrieve_knowledge start workflow_id=%s", state.get("workflow_id"))
    agent.vector_store.ensure_index()
    prd_text = read_prd_text(state, getattr(agent, "file_service", None))
    query = f"{state['project_name']}\n{prd_text[:2000]}"

    # workflow 的 transform 策略从 settings 取（创建 workflow 时快照进 settings）
    from ...query_transform import retrieve_with_transform
    from ...models.enums import QueryTransformStrategy
    strategy = getattr(agent.settings, "workflow_query_transform_strategy", QueryTransformStrategy.NONE)
    llm = getattr(agent, "llm", None)
    result = retrieve_with_transform(query, strategy, agent.vector_store, agent.settings, llm, top_k=state.get("top_k"))
    sources = result.chunks

    step_meta = {"source_paths": [s.path for s in sources]}
    if result.degraded:
        step_meta["transform_degraded"] = result.degrade_reason

    agent.logger.info("step retrieve_knowledge finish workflow_id=%s sources=%s degraded=%s",
                      state.get("workflow_id"), len(sources), result.degraded)
    return {
        "sources": sources,
        "current_step": "retrieve",
        **agent._step("retrieve_knowledge", f"检索到 {len(sources)} 个知识库片段", step_meta),
    }
```

- [ ] **Step 4: 写测试**

在 `test_chat_dispatch.py`（或现有 chat 接入测试）加一个验证 transform_degraded 传递的测试：mock `retrieve_with_transform` 返回 degraded=True，断言 ChatResponse.transform_degraded 非空。参照现有 chat 测试的 monkeypatch 模式。

```python
def test_chat_transform_degraded_propagates(client, monkeypatch):
    from porto_chatbot.query_transform import TransformResult
    monkeypatch.setattr(
        "porto_chatbot.agent.langchain_chat.retrieve_with_transform",
        lambda *a, **k: TransformResult([], degraded=True, degrade_reason="llm_call_failed"),
    )
    # 触发一次 RAG chat（routing off 或 rag intent）
    resp = client.post("/api/chat", json={"message": "查知识库", "session_id": "t"})
    # transform_degraded 应出现在响应里（具体断言按实际 ChatResponse 序列化）
```

- [ ] **Step 5: 运行测试 + 全量回归**

Run: `cd backend && uv run pytest -q`
Expected: 全过。重点确认现有 chat 测试不破（默认 binary+none = 走 store.search）。

- [ ] **Step 6: Commit**

```bash
git add backend/src/porto_chatbot/models/chat.py backend/src/porto_chatbot/agent/langchain_chat.py backend/src/porto_chatbot/agent/nodes/retrieve.py backend/tests/test_chat_dispatch.py
git commit -m "feat: wire routing+transform into chat/workflow with degraded visibility"
```

---

### Task 9: 前端（types + api + retrieval_optimization tab + StrategyCardGroup + 两表单）

**Files:**
- Modify: `frontend/src/lib/types.ts`
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/components/porto-workbench.tsx`
- 验证: `cd frontend && npm run build`

**Interfaces:**
- Consumes: Task 3 的 `/api/settings` 新子对象
- Produces: 检索优化 tab + 横向卡片选择器

> ⚠️ 写前端代码前先读 `frontend/node_modules/next/dist/docs/` 相关 guide（AGENTS.md 警告此 Next.js 有 breaking changes）。

- [ ] **Step 1: types.ts 加类型**

在 `types.ts` 的 `AppSettings`（约 line 111）相关定义处，参照现有 `RagConfig` 模式加：

```typescript
export type RagChatConfig = {
  intent_routing_mode: "off" | "binary" | "adaptive";
  query_transform_strategy: "none" | "hyde" | "multi_query" | "decomposition" | "step_back";
  multi_query_count: number;
  hyde_fallback_threshold: number;
};

export type RagWorkflowConfig = {
  query_transform_strategy: "none" | "multi_query" | "decomposition";
  multi_query_count: number;
};
```

`AppSettings` 类型加 `rag_chat: RagChatConfig; rag_workflow: RagWorkflowConfig;`。

- [ ] **Step 2: api.ts 透传**

`getAppSettings`/`saveAppSettings` 已透传整个 AppSettings 对象，确认返回类型含新字段即可（通常无需改，除非有显式字段拷贝）。

- [ ] **Step 3: StrategyCardGroup 组件**

在 `porto-workbench.tsx` 加可复用的横向卡片组件：

```tsx
type CardOption = { value: string; label: string; description: string };

function StrategyCardGroup({
  options, value, onChange,
}: {
  options: CardOption[]; value: string; onChange: (v: string) => void;
}) {
  return (
    <div className="flex flex-wrap gap-3">
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          onClick={() => onChange(opt.value)}
          className={`flex-1 min-w-[140px] rounded-lg border p-3 text-left transition ${
            value === opt.value
              ? "border-zinc-900 bg-zinc-50 ring-1 ring-zinc-900"
              : "border-zinc-200 hover:border-zinc-400"
          }`}
        >
          <div className="text-sm font-semibold text-zinc-900">{opt.label}</div>
          <div className="mt-1 text-xs text-zinc-500">{opt.description}</div>
        </button>
      ))}
    </div>
  );
}
```

- [ ] **Step 4: 两表单 + tab**

在 `SettingsSection` 类型加 `"retrieval_optimization"`。在 `SettingsPage` 的 tab 列表加一项。新增 `RagOptimizationSettingsForm`（含 Chat 场景 + Workflow 场景两组卡片），参照现有 `RagSettingsForm` 的 `useState` + `onSave` 模式。

卡片选项常量（标题英文 + 介绍中文）：

```tsx
const ROUTING_OPTIONS: CardOption[] = [
  { value: "off", label: "Off", description: "不做意图分流，所有消息都查知识库" },
  { value: "binary", label: "Binary", description: "自动区分闲聊与知识库问答；闲聊直答，其余查库" },
  { value: "adaptive", label: "Adaptive", description: "三级分流——闲聊直答 / 快速检索 / 深度检索（自动套用查询变换）" },
];

const CHAT_TRANSFORM_OPTIONS: CardOption[] = [
  { value: "none", label: "None", description: "直接用原始问题检索" },
  { value: "hyde", label: "HyDE", description: "先生成假设性答案再检索，弥补问题与文档的措辞差异（+1 次模型调用）" },
  { value: "multi_query", label: "Multi-Query", description: "生成多个改写问题分别检索后融合，提升召回" },
  { value: "decomposition", label: "Decomposition", description: "将复杂问题拆成子问题分别检索，适合多跳追问" },
  { value: "step_back", label: "Step-Back", description: "先抽象出更高层问题，检索背景知识" },
];

const WORKFLOW_TRANSFORM_OPTIONS: CardOption[] = [
  { value: "none", label: "None", description: "直接用原始问题检索" },
  { value: "multi_query", label: "Multi-Query", description: "生成多个改写问题分别检索后融合，提升召回" },
  { value: "decomposition", label: "Decomposition", description: "将复杂问题拆成子问题分别检索，适合多跳追问" },
];
```

选中 `multi_query` 时展开改写数量滑块（`<input type="range" min={2} max={8}>`）。tab 顶部提示："查询优化为查询时行为，修改后立即生效，无需重建知识库索引。"

表单保存调用 `saveAppSettings({ rag_chat: {...} })` / `{ rag_workflow: {...} }`。

- [ ] **Step 5: build 验证**

Run: `cd frontend && npm run build`
Expected: 编译通过（若有 tsc 假阳性，参考 [[rtk-wrapper-unreliable-typecheck]]，以 build 结果为准）

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/types.ts frontend/src/lib/api.ts frontend/src/components/porto-workbench.tsx
git commit -m "feat: retrieval optimization tab with strategy card selector"
```

---

## Self-Review

**Spec coverage（逐条对照）：**
- 5 个 transform 策略 → Task 5(none+降级) + Task 6(hyde/multi_query/decomposition/step_back) ✅
- 3 个 routing 模式 → Task 7 ✅
- chat/workflow 场景分离 → Task 1(settings) + Task 2(config_store namespace) + Task 3(API) ✅
- 横向卡片 + 英文标题 + 中文介绍 → Task 9 ✅
- 默认值零行为变化 → 每个 task Step 都跑全量回归 ✅
- 降级 fail-open + 可见 → Task 5(TransformResult.degraded) + Task 8(ChatResponse.transform_degraded) ✅
- vector_store 拆 _search_raw → Task 4 ✅
- routing 仅 chat → Task 8（workflow retrieve 无 routing）✅

**已知需实现时确认的点（非占位符，是实现细节微调）：**
1. `store_with_docs` fixture：`test_vector_store.py` 是否已有，需提取到 `conftest.py` 供 `test_query_transform.py` 共享。
2. `_rrf_fuse` 里 SourceChunk 重建方式：若 pydantic 模型用 `model_copy(update=)`，若 dataclass 用 `{**chunk, ...}`——实现时看 `models/common.py`。
3. Task 8 的 `langchain_chat.py` 控制流改造需仔细对照原 line 161-175，确保 direct 分支与流式分支（`chat_stream`）都正确处理。**注意：若 `langchain_chat.py` 有流式版本（`chat_stream`）也调 route_chat_intent，需同步改。**
4. Task 8 retrieve 节点的 `agent.settings`/`agent.llm` 属性名需对照实际 agent 对象确认。

**类型一致性：** `retrieve_with_transform` 签名（query, strategy, store, settings, llm, top_k）在 Task 5 定义，Task 6/8 调用处一致 ✅。`TransformResult.chunks/degraded/degrade_reason` 在 Task 5 定义，Task 8 调用处一致 ✅。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-07-rag-query-optimization.md`. Two execution options:

**1. Subagent-Driven (recommended)** - 每个 task 派 fresh subagent，task 间 review，迭代快

**2. Inline Execution** - 在当前会话用 executing-plans 批量执行 + checkpoint

哪种？
