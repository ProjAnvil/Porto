# Embedding & Reranking 模型扩展 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 接入 OpenAI-compatible embedding API（覆盖 Jina/Voyage/Fireworks/TEI/vLLM）和专用 cross-encoder reranker（Jina/Cohere/Voyage /v1/rerank），用 Strategy Pattern + Registry 替代 if-else 链。

**Architecture:** EmbeddingClient 和 rerank_chunks 各持一个 `{EnumValue: BackendClass}` 注册表，按配置选择后端类实例化委托调用。新增 provider 只需创建一个 backend 类 + 注册表加一行。前端设置页增加新 provider 选项和 rerank type 选择器。

**Tech Stack:** Python 3.14, FastAPI, Pydantic, openai SDK, httpx, pytest; Next.js, TypeScript, React

## Global Constraints

- 向后兼容：`embedding_provider` 默认 `LOCAL`，`rerank_type` 默认 `LLM`，`rerank_enabled` 默认 `False`
- 新字段默认 `None`（Pydantic `exclude_none=True` 序列化忽略）
- fail-open：cross-encoder rerank 失败返回原始 chunks，不阻断检索
- fail-hard：embedding 失败 raise（无法继续索引/检索）
- 不新增 Python 依赖（`openai` 和 `httpx` 已在 pyproject.toml）
- 前端 Next.js 有 breaking changes，需参考 `node_modules/next/dist/docs/`
- API key 不出现在日志中（`SENSITIVE_SETTING_KEYS`）

---

## File Structure

| 文件 | 职责 | 动作 |
|------|------|------|
| `backend/src/porto_chatbot/models/enums.py` | 枚举定义 | 修改：+`OPENAI_COMPATIBLE`、+`RerankType` |
| `backend/src/porto_chatbot/settings.py` | 配置字段 | 修改：+4 字段 |
| `backend/src/porto_chatbot/embeddings.py` | Embedding 后端 | 修改：重构为 Strategy + Registry |
| `backend/src/porto_chatbot/rerankers.py` | Reranker 后端 | **新建**：LLMReranker + CrossEncoderReranker + registry |
| `backend/src/porto_chatbot/retrieval.py` | 检索编排 | 修改：rerank_chunks 改为 registry dispatch |
| `backend/src/porto_chatbot/models/payload.py` | API Payload | 修改：+4 字段 |
| `backend/src/porto_chatbot/config_store.py` | 持久化 | 修改：+4 key + 2 sensitive key |
| `backend/src/porto_chatbot/api/deps.py` | 默认值 | 修改：default_rag_settings() +4 字段 |
| `backend/src/porto_chatbot/health.py` | 健康探测 | 修改：+OPENAI_COMPATIBLE probe |
| `backend/tests/test_embeddings.py` | Embedding 测试 | **新建** |
| `backend/tests/test_rerankers.py` | Reranker 测试 | **新建** |
| `backend/tests/test_settings_fields.py` | Settings 测试 | 修改：+新字段断言 |
| `backend/tests/test_settings_api.py` | API 测试 | 修改：+新字段往返 |
| `backend/tests/test_health_embedding.py` | Health 测试 | **新建** |
| `frontend/src/lib/types.ts` | TS 类型 | 修改：RagConfig 扩展 |
| `frontend/src/lib/api.ts` | 默认值 | 修改：defaultRagConfig 扩展 |
| `frontend/src/components/porto-workbench.tsx` | 设置 UI | 修改：+选项 +条件字段 |

---

### Task 1: Enums — `EmbeddingProvider.OPENAI_COMPATIBLE` + `RerankType`

**Files:**
- Modify: `backend/src/porto_chatbot/models/enums.py`
- Test: `backend/tests/test_settings_fields.py`

**Interfaces:**
- Produces: `EmbeddingProvider.OPENAI_COMPATIBLE`（值 `"openai_compatible"`）、`RerankType`（`"llm"` / `"cross_encoder"`）

- [ ] **Step 1: Write the failing test**

在 `backend/tests/test_settings_fields.py` 末尾追加：

```python
def test_new_enums():
    from porto_chatbot.models.enums import EmbeddingProvider, RerankType
    assert EmbeddingProvider.OPENAI_COMPATIBLE == "openai_compatible"
    assert RerankType.LLM == "llm"
    assert RerankType.CROSS_ENCODER == "cross_encoder"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_settings_fields.py::test_new_enums -v`
Expected: FAIL with `AttributeError: cannot access member 'OPENAI_COMPATIBLE'` / `cannot import name 'RerankType'`

- [ ] **Step 3: Add enum values**

在 `backend/src/porto_chatbot/models/enums.py` 的 `EmbeddingProvider` 类中新增一行：

```python
class EmbeddingProvider(StrEnum):
    LOCAL = "local"
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"
```

在 `EmbeddingProvider` 类之后新增 `RerankType` 枚举（放在 `LLMProvider` 之后）：

```python
# ── Reranker 类型（LLM 提示重排 vs 专用 cross-encoder）──
class RerankType(StrEnum):
    LLM = "llm"
    CROSS_ENCODER = "cross_encoder"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_settings_fields.py::test_new_enums -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/models/enums.py backend/tests/test_settings_fields.py
git commit -m "feat(enums): add EmbeddingProvider.OPENAI_COMPATIBLE and RerankType enum"
```

---

### Task 2: Settings — 4 个新配置字段

**Files:**
- Modify: `backend/src/porto_chatbot/settings.py`
- Test: `backend/tests/test_settings_fields.py`

**Interfaces:**
- Consumes: `RerankType` from Task 1
- Produces: `settings.embedding_api_key`、`settings.rerank_type`、`settings.rerank_api_key`、`settings.rerank_base_url`

- [ ] **Step 1: Write the failing test**

在 `backend/tests/test_settings_fields.py` 追加：

```python
def test_new_rag_settings_defaults():
    s = Settings()
    assert s.embedding_api_key is None
    assert s.rerank_type == "llm"
    assert s.rerank_api_key is None
    assert s.rerank_base_url is None


def test_new_rag_settings_custom():
    s = Settings(
        embedding_api_key="sk-test",
        rerank_type="cross_encoder",
        rerank_api_key="jina-key",
        rerank_base_url="https://api.jina.ai/v1",
    )
    assert s.embedding_api_key == "sk-test"
    assert s.rerank_type == "cross_encoder"
    assert s.rerank_api_key == "jina-key"
    assert s.rerank_base_url == "https://api.jina.ai/v1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_settings_fields.py::test_new_rag_settings_defaults tests/test_settings_fields.py::test_new_rag_settings_custom -v`
Expected: FAIL with `AttributeError` / `ValidationError`

- [ ] **Step 3: Add fields to Settings**

在 `backend/src/porto_chatbot/settings.py` 中：

首先在 import 块中加入 `RerankType`：

```python
from .models.enums import (
    ChatbotBackend,
    DocumentParseMode,
    EmbeddingProvider,
    IntentRoutingMode,
    LLMProvider,
    LocalParser,
    QueryTransformStrategy,
    RerankType,
    RetrievalMethod,
)
```

在 `embedding_base_url` 字段后（第 38 行附近）添加：

```python
    embedding_api_key: str | None = None
```

在 `rerank_choice_batch_size` 字段后（第 144 行附近的 rerank 区块）添加：

```python
    rerank_type: RerankType = RerankType.LLM
    rerank_api_key: str | None = None
    rerank_base_url: str | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_settings_fields.py -v`
Expected: PASS（所有测试包括之前的）

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/settings.py backend/tests/test_settings_fields.py
git commit -m "feat(settings): add embedding_api_key, rerank_type, rerank_api_key, rerank_base_url"
```

---

### Task 3: Embedding Strategy + Registry 重构

**Files:**
- Modify: `backend/src/porto_chatbot/embeddings.py`
- Test: `backend/tests/test_embeddings.py`（新建）

**Interfaces:**
- Consumes: `EmbeddingProvider` enum, `Settings`
- Produces: `EmbeddingBackend` protocol, `LocalEmbeddingBackend`, `OllamaEmbeddingBackend`, `OpenAICompatibleEmbeddingBackend`, `EMBEDDING_BACKENDS` registry, `EmbeddingClient`（facade）

- [ ] **Step 1: Write tests for all three backends + registry dispatch**

创建 `backend/tests/test_embeddings.py`：

```python
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from porto_chatbot.embeddings import (
    EMBEDDING_BACKENDS,
    EmbeddingClient,
    LocalEmbeddingBackend,
    OllamaEmbeddingBackend,
    OpenAICompatibleEmbeddingBackend,
)
from porto_chatbot.models.enums import EmbeddingProvider
from porto_chatbot.settings import Settings


def _make_settings(tmp_path, **overrides) -> Settings:
    defaults = dict(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        embedding_dimensions=128,
        embedding_provider="local",
    )
    defaults.update(overrides)
    return Settings(**defaults)


# ── Registry ──

def test_registry_has_all_providers():
    assert set(EMBEDDING_BACKENDS.keys()) == {
        EmbeddingProvider.LOCAL,
        EmbeddingProvider.OLLAMA,
        EmbeddingProvider.OPENAI_COMPATIBLE,
    }


# ── LocalEmbeddingBackend ──

def test_local_backend_embed(tmp_path):
    settings = _make_settings(tmp_path, embedding_provider="local", embedding_dimensions=64)
    backend = LocalEmbeddingBackend(settings)
    vectors = backend.embed_documents(["hello world", "支付风控"])
    assert len(vectors) == 2
    assert len(vectors[0]) == 64
    assert len(vectors[1]) == 64


# ── OllamaEmbeddingBackend ──

def test_ollama_backend_embed(tmp_path):
    settings = _make_settings(
        tmp_path,
        embedding_provider="ollama",
        embedding_model="qwen3-embedding:0.6b",
        embedding_base_url="http://localhost:11434",
    )
    backend = OllamaEmbeddingBackend(settings)
    mock_client = MagicMock()
    mock_client.embed.return_value = {"embeddings": [[0.1, 0.2], [0.3, 0.4]]}
    backend._client = mock_client
    vectors = backend.embed_documents(["text1", "text2"])
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    mock_client.embed.assert_called_once_with(model="qwen3-embedding:0.6b", input=["text1", "text2"])


# ── OpenAICompatibleEmbeddingBackend ──

def test_openai_compatible_backend_embed(tmp_path):
    settings = _make_settings(
        tmp_path,
        embedding_provider="openai_compatible",
        embedding_model="text-embedding-3-small",
        embedding_base_url="https://api.openai.com/v1",
        embedding_api_key="sk-test",
    )
    mock_resp = MagicMock()
    mock_resp.data = [
        MagicMock(embedding=[0.1, 0.2, 0.3]),
        MagicMock(embedding=[0.4, 0.5, 0.6]),
    ]
    mock_client = MagicMock()
    mock_client.embeddings.create.return_value = mock_resp
    with patch("openai.OpenAI", return_value=mock_client):
        backend = OpenAICompatibleEmbeddingBackend(settings)
    vectors = backend.embed_documents(["hello", "world"])
    assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    backend._client.embeddings.create.assert_called_once_with(
        model="text-embedding-3-small", input=["hello", "world"]
    )


# ── EmbeddingClient facade dispatch ──

def test_client_dispatches_to_local(tmp_path):
    settings = _make_settings(tmp_path, embedding_provider="local", embedding_dimensions=32)
    client = EmbeddingClient(settings)
    vec = client.embed_query("test text")
    assert len(vec) == 32


def test_client_dispatches_to_openai_compatible(tmp_path):
    settings = _make_settings(
        tmp_path,
        embedding_provider="openai_compatible",
        embedding_model="jina-embeddings-v3",
        embedding_base_url="https://api.jina.ai/v1",
        embedding_api_key="jina-test",
    )
    mock_resp = MagicMock()
    mock_resp.data = [MagicMock(embedding=[0.1, 0.2])]
    mock_client = MagicMock()
    mock_client.embeddings.create.return_value = mock_resp
    with patch("openai.OpenAI", return_value=mock_client):
        client = EmbeddingClient(settings)
    vec = client.embed_query("test")
    assert vec == [0.1, 0.2]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_embeddings.py -v`
Expected: FAIL — `ImportError` for `EMBEDDING_BACKENDS`, `LocalEmbeddingBackend`, etc.

- [ ] **Step 3: Rewrite embeddings.py with Strategy + Registry**

将 `backend/src/porto_chatbot/embeddings.py` 完整替换为：

```python
from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

import ollama

from .logging_utils import get_component_logger
from .models.enums import EmbeddingProvider
from .settings import Settings

TOKEN_RE = re.compile(r"[\w一-鿿]+", re.UNICODE)


def tokens(text: str) -> list[str]:
    result: list[str] = []
    for raw in TOKEN_RE.findall(text):
        token = raw.lower()
        result.append(token)
        cjk_chars = [ch for ch in token if "一" <= ch <= "鿿"]
        if cjk_chars:
            result.extend(cjk_chars)
            result.extend("".join(cjk_chars[i : i + 2]) for i in range(len(cjk_chars) - 1))
    return result


def local_embed_text(text: str, dimensions: int = 384) -> list[float]:
    vector = [0.0] * dimensions
    for token in tokens(text):
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[bucket] += sign
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0:
        return vector
    return [v / norm for v in vector]


class EmbeddingBackend(Protocol):
    """Embedding 后端接口。"""

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...


class LocalEmbeddingBackend:
    """Hash 散列 embedding，无需外部依赖。"""

    def __init__(self, settings: Settings):
        self._dimensions = settings.embedding_dimensions

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [local_embed_text(text, self._dimensions) for text in texts]


class OllamaEmbeddingBackend:
    """Ollama 本地推理 embedding。"""

    def __init__(self, settings: Settings):
        self._client = ollama.Client(host=settings.embedding_base_url)
        self._model = settings.embedding_model

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        response = self._client.embed(model=self._model, input=list(texts))
        return [list(vector) for vector in response["embeddings"]]


class OpenAICompatibleEmbeddingBackend:
    """OpenAI-compatible embedding API。

    覆盖 OpenAI / Jina / Voyage(OpenAI模式) / Fireworks / TEI / vLLM 等。
    """

    def __init__(self, settings: Settings):
        from openai import OpenAI

        self._client = OpenAI(
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
        )
        self._model = settings.embedding_model

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        response = self._client.embeddings.create(
            model=self._model,
            input=list(texts),
        )
        return [d.embedding for d in response.data]


EMBEDDING_BACKENDS: dict[EmbeddingProvider, type[EmbeddingBackend]] = {
    EmbeddingProvider.LOCAL: LocalEmbeddingBackend,
    EmbeddingProvider.OLLAMA: OllamaEmbeddingBackend,
    EmbeddingProvider.OPENAI_COMPATIBLE: OpenAICompatibleEmbeddingBackend,
}


class EmbeddingClient:
    """Facade：按配置选择后端，委托调用。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = get_component_logger("embeddings", settings)
        backend_cls = EMBEDDING_BACKENDS.get(settings.embedding_provider)
        if backend_cls is None:
            raise ValueError(
                f"Unsupported embedding provider: {settings.embedding_provider}"
            )
        self._backend = backend_cls(settings)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.logger.info(
            "embed documents provider=%s model=%s count=%s",
            self.settings.embedding_provider,
            self.settings.embedding_model,
            len(texts),
        )
        try:
            return self._backend.embed_documents(texts)
        except Exception:
            self.logger.exception(
                "embedding failed provider=%s model=%s base_url=%s count=%s",
                self.settings.embedding_provider,
                self.settings.embedding_model,
                self.settings.embedding_base_url,
                len(texts),
            )
            raise

    def embed_query(self, text: str) -> list[float]:
        self.logger.info(
            "embed query provider=%s chars=%s", self.settings.embedding_provider, len(text)
        )
        return self._backend.embed_documents([text])[0]
```

- [ ] **Step 4: Run new tests**

Run: `cd backend && python -m pytest tests/test_embeddings.py -v`
Expected: PASS

- [ ] **Step 5: Run existing tests for regression**

Run: `cd backend && python -m pytest tests/test_vector_store.py -v`
Expected: PASS（EmbeddingClient 接口不变，现有 LOCAL provider 行为零变化）

- [ ] **Step 6: Commit**

```bash
git add backend/src/porto_chatbot/embeddings.py backend/tests/test_embeddings.py
git commit -m "refactor(embeddings): Strategy Pattern + Registry for embedding backends"
```

---

### Task 4: Rerankers — 新文件 rerankers.py（LLM 提取 + Protocol + Registry）

**Files:**
- Create: `backend/src/porto_chatbot/rerankers.py`
- Test: `backend/tests/test_rerankers.py`（新建）

**Interfaces:**
- Consumes: `Settings`, `SourceChunk`, `LLMRerank` from llama_index, `_build_rerank_llm` logic from retrieval.py
- Produces: `RerankerBackend` protocol, `LLMReranker`, `RERANKER_BACKENDS` registry（此 task 只含 LLM）

- [ ] **Step 1: Write test for LLMReranker extraction and registry**

创建 `backend/tests/test_rerankers.py`：

```python
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from porto_chatbot.models.common import SourceChunk
from porto_chatbot.models.enums import RerankType
from porto_chatbot.rerankers import LLMReranker, RERANKER_BACKENDS
from porto_chatbot.settings import Settings


def _make_chunks(n: int = 3) -> list[SourceChunk]:
    return [
        SourceChunk(id=f"c{i}", path=f"doc{i}.md", title=f"Doc {i}", text=f"内容{i}", score=0.5, metadata={})
        for i in range(n)
    ]


def test_registry_has_llm():
    assert RerankType.LLM in RERANKER_BACKENDS


def test_llm_reranker_disabled_returns_original():
    """rerank_enabled=False 时 rerank_chunks 应原样返回（在 retrieval.py 层拦截）。"""
    # 此测试验证 LLMReranker 类本身可构造
    settings = Settings(
        data_dir="/tmp/test-rerank",
        log_dir="/tmp/test-rerank/logs",
        rerank_enabled=True,
        rerank_type="llm",
        agent_api_key=None,
    )
    # 缺 api_key 时 _build_rerank_llm 返回 None，LLMReranker._llm 为 None
    reranker = LLMReranker(settings)
    assert reranker._llm is None


def test_llm_reranker_passthrough_when_no_llm():
    """LLM 不可用时 reranker.rerank() 原样返回 chunks。"""
    settings = Settings(
        data_dir="/tmp/test-rerank",
        log_dir="/tmp/test-rerank/logs",
        rerank_enabled=True,
        rerank_type="llm",
    )
    reranker = LLMReranker(settings)
    chunks = _make_chunks()
    result = reranker.rerank(chunks, "test query")
    assert result is chunks  # 原样返回
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_rerankers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'porto_chatbot.rerankers'`

- [ ] **Step 3: Create rerankers.py with LLMReranker + protocol + registry**

创建 `backend/src/porto_chatbot/rerankers.py`：

```python
"""Reranker 后端：Strategy Pattern + Registry。

从 retrieval.py 提取 LLM rerank 逻辑，新增 cross-encoder reranker，
通过 ``RERANKER_BACKENDS`` 注册表按 ``rerank_type`` dispatch。
"""
from __future__ import annotations

from typing import Protocol

from llama_index.core import QueryBundle
from llama_index.core.schema import NodeWithScore, TextNode

from .logging_utils import get_component_logger
from .models import SourceChunk
from .models.enums import LLMProvider, RerankType
from .settings import Settings

logger = get_component_logger("rerankers")


def _build_rerank_llm(settings: Settings):
    """按 rerank_* 配置（缺省回退到 agent_*）构建 llama-index LLM。

    从 retrieval.py 原样提取，零行为变化。
    """
    provider = settings.rerank_provider or settings.agent_provider
    model = settings.rerank_model or settings.agent_model
    api_key = settings.agent_api_key
    if not api_key:
        return None
    try:
        if provider == LLMProvider.OPENAI:
            if settings.agent_base_url:
                from llama_index.llms.openai_like import OpenAILike

                return OpenAILike(
                    model=model,
                    api_key=api_key,
                    api_base=settings.agent_base_url,
                    is_chat_model=True,
                    is_function_calling_model=False,
                    context_window=max(settings.agent_max_tokens * 4, 4096),
                    temperature=0.0,
                )
            from llama_index.llms.openai import OpenAI

            return OpenAI(model=model, api_key=api_key, temperature=0.0)
        if provider == LLMProvider.ANTHROPIC:
            from llama_index.llms.anthropic import Anthropic

            return Anthropic(
                model=model,
                api_key=api_key,
                base_url=settings.agent_base_url or None,
            )
    except Exception:
        logger.exception("rerank llm build failed provider=%s model=%s", provider, model)
        return None
    logger.warning("rerank llm unsupported provider=%s", provider)
    return None


class RerankerBackend(Protocol):
    """Reranker 后端接口。"""

    def rerank(self, chunks: list[SourceChunk], query: str) -> list[SourceChunk]: ...


class LLMReranker:
    """LLM 提示重排（llama-index LLMRerank）。

    从 retrieval.py 的 rerank_chunks() + _build_rerank_llm() 提取。
    fail-open：LLM 不可用时原样返回 chunks。
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._llm = _build_rerank_llm(settings)
        self._top_n = settings.rerank_top_n
        self._batch_size = settings.rerank_choice_batch_size

    def rerank(self, chunks: list[SourceChunk], query: str) -> list[SourceChunk]:
        if self._llm is None:
            logger.info("rerank skipped reason=llm_unavailable")
            return chunks
        try:
            from llama_index.core.postprocessor import LLMRerank

            nodes = [
                NodeWithScore(
                    node=TextNode(text=c.text, id_=c.id, metadata=c.metadata),
                    score=c.score,
                )
                for c in chunks
            ]
            top_n = min(self._top_n, len(nodes))
            reranker = LLMRerank(
                llm=self._llm,
                top_n=top_n,
                choice_batch_size=self._batch_size,
            )
            reranked = reranker.postprocess_nodes(
                nodes, query_bundle=QueryBundle(query_str=query)
            )
        except Exception:
            logger.exception(
                "rerank failed query_chars=%s candidates=%s", len(query), len(chunks)
            )
            return chunks

        by_id = {c.id: c for c in chunks}
        result: list[SourceChunk] = []
        for node_with_score in reranked:
            original = by_id.get(node_with_score.node.id_)
            if original is None:
                continue
            result.append(
                SourceChunk(
                    id=original.id,
                    path=original.path,
                    title=original.title,
                    text=original.text,
                    score=round(float(node_with_score.score or 0.0), 4),
                    metadata=original.metadata,
                )
            )
        logger.info("rerank finish candidates=%s kept=%s", len(chunks), len(result))
        return result or chunks


RERANKER_BACKENDS: dict[RerankType, type[RerankerBackend]] = {
    RerankType.LLM: LLMReranker,
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_rerankers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/rerankers.py backend/tests/test_rerankers.py
git commit -m "feat(rerankers): extract LLMReranker to rerankers.py with Strategy + Registry"
```

---

### Task 5: CrossEncoderReranker — 专用 cross-encoder 后端

**Files:**
- Modify: `backend/src/porto_chatbot/rerankers.py`
- Test: `backend/tests/test_rerankers.py`

**Interfaces:**
- Consumes: `httpx`, `Settings.rerank_base_url`/`rerank_api_key`/`rerank_model`/`rerank_top_n`
- Produces: `CrossEncoderReranker` class, registered in `RERANKER_BACKENDS`

- [ ] **Step 1: Write tests for CrossEncoderReranker**

在 `backend/tests/test_rerankers.py` 追加：

```python
from unittest.mock import patch, MagicMock

from porto_chatbot.rerankers import CrossEncoderReranker


def test_registry_has_cross_encoder():
    assert RerankType.CROSS_ENCODER in RERANKER_BACKENDS


def test_cross_encoder_rerank_success():
    """mock httpx.post，验证 rerank 正常排序。"""
    settings = Settings(
        data_dir="/tmp/test-rerank",
        log_dir="/tmp/test-rerank/logs",
        rerank_type="cross_encoder",
        rerank_model="jina-reranker-v2-base-multilingual",
        rerank_base_url="https://api.jina.ai/v1",
        rerank_api_key="jina-test-key",
        rerank_top_n=2,
    )
    reranker = CrossEncoderReranker(settings)
    chunks = _make_chunks(3)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {
        "results": [
            {"index": 2, "relevance_score": 0.95},
            {"index": 0, "relevance_score": 0.80},
        ],
    }
    with patch("porto_chatbot.rerankers.httpx.post", return_value=mock_response):
        result = reranker.rerank(chunks, "test query")

    assert len(result) == 2
    assert result[0].id == "c2"  # relevance 0.95 排第一
    assert result[1].id == "c0"  # relevance 0.80 排第二
    assert result[0].score == 0.95
    assert result[1].score == 0.8


def test_cross_encoder_rerank_voyage_response_format():
    """Voyage API 用 data 而非 results 作为响应 key。"""
    settings = Settings(
        data_dir="/tmp/test-rerank",
        log_dir="/tmp/test-rerank/logs",
        rerank_type="cross_encoder",
        rerank_model="rerank-2",
        rerank_base_url="https://api.voyageai.com/v1",
        rerank_api_key="voyage-key",
        rerank_top_n=1,
    )
    reranker = CrossEncoderReranker(settings)
    chunks = _make_chunks(2)

    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {
        "data": [
            {"index": 1, "relevance_score": 0.90},
        ],
    }
    with patch("porto_chatbot.rerankers.httpx.post", return_value=mock_response):
        result = reranker.rerank(chunks, "query")

    assert len(result) == 1
    assert result[0].id == "c1"


def test_cross_encoder_rerank_fail_open():
    """httpx 异常时原样返回 chunks（fail-open）。"""
    settings = Settings(
        data_dir="/tmp/test-rerank",
        log_dir="/tmp/test-rerank/logs",
        rerank_type="cross_encoder",
        rerank_model="jina-reranker-v2-base-multilingual",
        rerank_base_url="https://api.jina.ai/v1",
        rerank_api_key="jina-key",
        rerank_top_n=2,
    )
    reranker = CrossEncoderReranker(settings)
    chunks = _make_chunks(3)

    with patch("porto_chatbot.rerankers.httpx.post", side_effect=Exception("network error")):
        result = reranker.rerank(chunks, "query")

    assert result is chunks  # 原样返回
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_rerankers.py::test_cross_encoder_rerank_success -v`
Expected: FAIL — `ImportError: cannot import name 'CrossEncoderReranker'`

- [ ] **Step 3: Add CrossEncoderReranker to rerankers.py**

在 `backend/src/porto_chatbot/rerankers.py` 的文件顶部 import 区追加：

```python
import httpx
```

在 `LLMReranker` 类之后、`RERANKER_BACKENDS` 之前新增：

```python
class CrossEncoderReranker:
    """专用 cross-encoder reranker（POST {base_url}/rerank）。

    兼容 Jina / Cohere / Voyage 的 /v1/rerank 协议。
    fail-open：任何异常返回原始 chunks。
    """

    def __init__(self, settings: Settings):
        self._base_url = settings.rerank_base_url
        self._api_key = settings.rerank_api_key
        self._model = settings.rerank_model
        self._top_n = settings.rerank_top_n
        self._timeout = settings.agent_request_timeout

    def rerank(self, chunks: list[SourceChunk], query: str) -> list[SourceChunk]:
        if not self._base_url or not self._api_key:
            logger.info("cross-encoder rerank skipped reason=missing_config")
            return chunks
        try:
            top_n = min(self._top_n, len(chunks))
            # Voyage API 要求 documents 为 [str]，Jina/Cohere 要求 [{text}]。
            # 按 base_url host 判断发送格式。
            is_voyage = "voyageai.com" in (self._base_url or "")
            documents = [c.text for c in chunks] if is_voyage else [{"text": c.text} for c in chunks]
            resp = httpx.post(
                f"{self._base_url}/rerank",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "query": query,
                    "documents": documents,
                    "top_n": top_n,
                },
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results") or data.get("data") or []
            if not results:
                logger.warning("cross-encoder rerank empty results")
                return chunks
            by_index = {i: c for i, c in enumerate(chunks)}
            reranked: list[SourceChunk] = []
            for item in results:
                idx = item.get("index")
                score = float(item.get("relevance_score", 0.0))
                original = by_index.get(idx)
                if original is None:
                    continue
                reranked.append(
                    SourceChunk(
                        id=original.id,
                        path=original.path,
                        title=original.title,
                        text=original.text,
                        score=round(score, 4),
                        metadata=original.metadata,
                    )
                )
            logger.info(
                "cross-encoder rerank finish candidates=%s kept=%s",
                len(chunks),
                len(reranked),
            )
            return reranked or chunks
        except Exception:
            logger.exception(
                "cross-encoder rerank failed query_chars=%s candidates=%s",
                len(query),
                len(chunks),
            )
            return chunks
```

在 `RERANKER_BACKENDS` 注册表中新增一行：

```python
RERANKER_BACKENDS: dict[RerankType, type[RerankerBackend]] = {
    RerankType.LLM: LLMReranker,
    RerankType.CROSS_ENCODER: CrossEncoderReranker,
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_rerankers.py -v`
Expected: PASS（全部）

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/rerankers.py backend/tests/test_rerankers.py
git commit -m "feat(rerankers): add CrossEncoderReranker for Jina/Cohere/Voyage /v1/rerank API"
```

---

### Task 6: retrieval.py — 用 Registry dispatch 替代原 rerank 逻辑

**Files:**
- Modify: `backend/src/porto_chatbot/retrieval.py`
- Test: `backend/tests/test_rerankers.py`（扩展回归测试）

**Interfaces:**
- Consumes: `RERANKER_BACKENDS` from rerankers.py
- Produces: `rerank_chunks()` 函数（签名不变，调用方零改动）

- [ ] **Step 1: Write regression test for rerank_chunks dispatch**

在 `backend/tests/test_rerankers.py` 追加：

```python
from porto_chatbot.retrieval import rerank_chunks


def test_rerank_chunks_disabled_returns_original():
    """rerank_enabled=False 时原样返回。"""
    settings = Settings(
        data_dir="/tmp/test-rerank",
        log_dir="/tmp/test-rerank/logs",
        rerank_enabled=False,
    )
    chunks = _make_chunks(3)
    result = rerank_chunks(chunks, "query", settings)
    assert result is chunks


def test_rerank_chunks_empty_returns_empty():
    settings = Settings(
        data_dir="/tmp/test-rerank",
        log_dir="/tmp/test-rerank/logs",
        rerank_enabled=True,
        rerank_type="llm",
    )
    assert rerank_chunks([], "query", settings) == []


def test_rerank_chunks_cross_encoder_dispatch():
    """rerank_type=cross_encoder 时走 CrossEncoderReranker（mock 验证）。"""
    settings = Settings(
        data_dir="/tmp/test-rerank",
        log_dir="/tmp/test-rerank/logs",
        rerank_enabled=True,
        rerank_type="cross_encoder",
        rerank_model="jina-reranker-v2",
        rerank_base_url="https://api.jina.ai/v1",
        rerank_api_key="key",
        rerank_top_n=2,
    )
    chunks = _make_chunks(3)
    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {
        "results": [
            {"index": 0, "relevance_score": 0.9},
            {"index": 1, "relevance_score": 0.8},
        ],
    }
    with patch("porto_chatbot.rerankers.httpx.post", return_value=mock_response):
        result = rerank_chunks(chunks, "query", settings)
    assert len(result) == 2
```

- [ ] **Step 2: Run test to verify current behavior (some will fail because old code still uses inline LLMRerank)**

Run: `cd backend && python -m pytest tests/test_rerankers.py::test_rerank_chunks_cross_encoder_dispatch -v`
Expected: FAIL — old `rerank_chunks` doesn't know about `rerank_type` / `cross_encoder`

- [ ] **Step 3: Simplify retrieval.py rerank_chunks + remove old code**

在 `backend/src/porto_chatbot/retrieval.py` 中：

1. 在 import 区添加：
```python
from .rerankers import RERANKER_BACKENDS
```

2. 删除 `_build_rerank_llm` 函数（已移至 rerankers.py）

3. 将 `rerank_chunks` 函数替换为：

```python
def rerank_chunks(chunks: list[SourceChunk], query: str, settings: Settings) -> list[SourceChunk]:
    """按 ``settings.rerank_type`` 选择 reranker 后端做二次精排。

    未启用 / 未配置 / 执行异常时均原样降级返回 ``chunks``（fail-open）。
    """
    if not settings.rerank_enabled or not chunks:
        return chunks
    backend_cls = RERANKER_BACKENDS.get(settings.rerank_type)
    if backend_cls is None:
        logger.warning("rerank skipped reason=unknown_type type=%s", settings.rerank_type)
        return chunks
    try:
        backend = backend_cls(settings)
        return backend.rerank(chunks, query)
    except Exception:
        logger.exception("rerank failed query_chars=%s candidates=%s", len(query), len(chunks))
        return chunks
```

4. 删除原有的 import（已不再需要）：
```python
# 删除：from llama_index.core.postprocessor import LLMRerank （这个 import 已在 rerankers.py 内部延迟导入）
# 删除：from .models.enums import LLMProvider（如果 retrieval.py 不再使用它）
```

注意：检查 retrieval.py 其他地方是否还使用 `LLMProvider`（hybrid_fusion_search 等不需要）。如果只有 `_build_rerank_llm` 用到，则删除该 import。

- [ ] **Step 4: Run all reranker tests**

Run: `cd backend && python -m pytest tests/test_rerankers.py -v`
Expected: PASS（全部）

- [ ] **Step 5: Run existing retrieval/vector_store regression**

Run: `cd backend && python -m pytest tests/test_vector_store.py tests/test_query_transform.py -v`
Expected: PASS（`rerank_chunks` 签名不变，调用方零改动）

- [ ] **Step 6: Commit**

```bash
git add backend/src/porto_chatbot/retrieval.py backend/tests/test_rerankers.py
git commit -m "refactor(retrieval): rerank_chunks dispatches via RERANKER_BACKENDS registry"
```

---

### Task 7: Payload + ConfigStore + Deps — 新字段贯通 API

**Files:**
- Modify: `backend/src/porto_chatbot/models/payload.py`
- Modify: `backend/src/porto_chatbot/config_store.py`
- Modify: `backend/src/porto_chatbot/api/deps.py`
- Test: `backend/tests/test_settings_api.py`

**Interfaces:**
- Consumes: `RerankType` enum from Task 1
- Produces: `RagSettingsPayload` 新字段、ConfigStore 持久化、deps 默认值

- [ ] **Step 1: Write API round-trip test**

在 `backend/tests/test_settings_api.py` 追加：

```python
def test_settings_save_rag_with_new_fields(client):
    resp = client.put(
        "/api/settings",
        json={
            "rag": {
                "embedding_provider": "openai_compatible",
                "embedding_api_key": "sk-test-123",
                "rerank_type": "cross_encoder",
                "rerank_base_url": "https://api.jina.ai/v1",
                "rerank_api_key": "jina-key",
            }
        },
    )
    assert resp.status_code == 200
    rag = resp.json()["rag"]
    assert rag["embedding_provider"] == "openai_compatible"
    assert rag["embedding_api_key"] == "sk-test-123"
    assert rag["rerank_type"] == "cross_encoder"
    assert rag["rerank_base_url"] == "https://api.jina.ai/v1"
    # api_key 不应在 GET 中回显（sensitive）
    persisted = client.get("/api/settings").json()["rag"]
    assert persisted["rerank_type"] == "cross_encoder"
```

注意：`SENSITIVE_SETTING_KEYS` 仅控制 `config_store.py` 日志脱敏（区分 safe_keys/redacted_keys），**不影响** db 写入和 GET 响应回显。API key 会正常持久化与返回（与现有 `agent_api_key` 行为一致）。上面的 `assert rag["embedding_api_key"] == "sk-test-123"` 断言是正确的。

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_settings_api.py::test_settings_save_rag_with_new_fields -v`
Expected: FAIL — response 中没有新字段

- [ ] **Step 3: Add fields to RagSettingsPayload**

在 `backend/src/porto_chatbot/models/payload.py` 的 `RagSettingsPayload` 中：

首先 import 区加入 `RerankType`：

```python
from .enums import (
    ChatbotBackend,
    DocumentParseMode,
    EmbeddingProvider,
    IntentRoutingMode,
    LLMProvider,
    LocalParser,
    QueryTransformStrategy,
    RerankType,
    RetrievalMethod,
)
```

在 `rerank_choice_batch_size` 字段之后追加：

```python
    embedding_api_key: str | None = None
    rerank_type: RerankType | None = None
    rerank_api_key: str | None = None
    rerank_base_url: str | None = None
```

- [ ] **Step 4: Add keys to ConfigStore**

在 `backend/src/porto_chatbot/config_store.py` 的 `RAG_SETTING_KEYS` set 中新增 4 个 key：

```python
RAG_SETTING_KEYS = {
    "embedding_provider",
    "embedding_model",
    "embedding_base_url",
    "embedding_api_key",          # 新增
    "chunk_size",
    "chunk_overlap",
    "top_k",
    "kb_dirs",
    "retrieval_method",
    "bm25_top_k",
    "hybrid_vector_weight",
    "rerank_enabled",
    "rerank_top_n",
    "rerank_type",                # 新增
    "rerank_provider",
    "rerank_model",
    "rerank_choice_batch_size",
    "rerank_api_key",             # 新增
    "rerank_base_url",            # 新增
}
```

在 `SENSITIVE_SETTING_KEYS` 中新增两个 key：

```python
SENSITIVE_SETTING_KEYS = {"agent_api_key", "critic_api_key", "embedding_api_key", "rerank_api_key"}
```

- [ ] **Step 5: Add defaults to deps.py**

在 `backend/src/porto_chatbot/api/deps.py` 的 `default_rag_settings()` 函数中，在 `RagSettingsPayload(...)` 构造内追加（在 `rerank_choice_batch_size` 之后）：

```python
        embedding_api_key=settings.embedding_api_key,
        rerank_type=settings.rerank_type,
        rerank_api_key=settings.rerank_api_key,
        rerank_base_url=settings.rerank_base_url,
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_settings_api.py -v`
Expected: PASS

- [ ] **Step 7: Run config_store regression**

Run: `cd backend && python -m pytest tests/test_config_store.py tests/test_settings_backend.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add backend/src/porto_chatbot/models/payload.py backend/src/porto_chatbot/config_store.py backend/src/porto_chatbot/api/deps.py backend/tests/test_settings_api.py
git commit -m "feat(payload): wire embedding_api_key, rerank_type, rerank_api_key, rerank_base_url through API"
```

---

### Task 8: Health Probe — OPENAI_COMPATIBLE embedding 探测

**Files:**
- Modify: `backend/src/porto_chatbot/health.py`
- Test: `backend/tests/test_health_embedding.py`（新建）

**Interfaces:**
- Consumes: `EmbeddingProvider.OPENAI_COMPATIBLE`, `Settings`
- Produces: `_openai_compatible_embed_ping()` static method

- [ ] **Step 1: Write health probe test**

创建 `backend/tests/test_health_embedding.py`：

```python
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from porto_chatbot.health import HealthMonitor
from porto_chatbot.models import DependencyName, DependencyStatus
from porto_chatbot.settings import Settings


def _make_settings(tmp_path, **overrides) -> Settings:
    defaults = dict(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_probe_openai_compatible_ok(tmp_path):
    settings = _make_settings(
        tmp_path,
        embedding_provider="openai_compatible",
        embedding_model="text-embedding-3-small",
        embedding_base_url="https://api.openai.com/v1",
        embedding_api_key="sk-test",
    )
    monitor = HealthMonitor(
        settings_provider=lambda: settings,
        rag_available=lambda: (True, None),
        rag_status=lambda: None,
    )
    health = monitor._probe_embedding(settings)
    assert health.name == DependencyName.EMBEDDING
    assert health.status == DependencyStatus.OK


def test_probe_openai_compatible_down(tmp_path):
    settings = _make_settings(
        tmp_path,
        embedding_provider="openai_compatible",
        embedding_model="text-embedding-3-small",
        embedding_base_url="https://invalid.example.com/v1",
        embedding_api_key="bad-key",
    )
    monitor = HealthMonitor(
        settings_provider=lambda: settings,
        rag_available=lambda: (True, None),
        rag_status=lambda: None,
    )
    with patch("openai.OpenAI") as mock_cls:
        mock_cls.return_value.embeddings.create.side_effect = Exception("401 Unauthorized")
        health = monitor._probe_embedding(settings)
    assert health.name == DependencyName.EMBEDDING
    assert health.status == DependencyStatus.DOWN


def test_probe_local_ok(tmp_path):
    """LOCAL provider 探测恒 OK（回归）。"""
    settings = _make_settings(tmp_path, embedding_provider="local")
    monitor = HealthMonitor(
        settings_provider=lambda: settings,
        rag_available=lambda: (True, None),
        rag_status=lambda: None,
    )
    health = monitor._probe_embedding(settings)
    assert health.status == DependencyStatus.OK
```

注意：`test_probe_openai_compatible_ok` 需要网络或 mock。由于 HealthMonitor 用 ThreadPoolExecutor 调用，mock 方案是 patch openai.OpenAI 的 embeddings.create。如果测试环境无网络，改为：

```python
def test_probe_openai_compatible_ok(tmp_path):
    settings = _make_settings(
        tmp_path,
        embedding_provider="openai_compatible",
        embedding_model="text-embedding-3-small",
        embedding_base_url="https://api.openai.com/v1",
        embedding_api_key="sk-test",
    )
    monitor = HealthMonitor(
        settings_provider=lambda: settings,
        rag_available=lambda: (True, None),
        rag_status=lambda: None,
    )
    mock_resp = MagicMock()
    mock_resp.data = [MagicMock(embedding=[0.1, 0.2])]
    with patch("openai.OpenAI") as mock_cls:
        mock_cls.return_value.embeddings.create.return_value = mock_resp
        health = monitor._probe_embedding(settings)
    assert health.status == DependencyStatus.OK
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_health_embedding.py -v`
Expected: FAIL — `_probe_embedding` 不认识 `OPENAI_COMPATIBLE` provider，走 OLLAMA 分支或报错

- [ ] **Step 3: Add OPENAI_COMPATIBLE probe to health.py**

在 `backend/src/porto_chatbot/health.py` 的 `_probe_embedding` 方法中，在 OLLAMA 的 try 块之前添加 OPENAI_COMPATIBLE 分支：

```python
    def _probe_embedding(self, settings: Settings) -> DependencyHealth:
        name = DependencyName.EMBEDDING
        if settings.embedding_provider == EmbeddingProvider.LOCAL:
            return DependencyHealth(
                name=name, status=DependencyStatus.OK, detail="local", checked_at=_now_iso()
            )
        if settings.embedding_provider == EmbeddingProvider.OPENAI_COMPATIBLE:
            try:
                latency = self._executor.submit(
                    self._openai_compatible_embed_ping,
                    settings.embedding_base_url,
                    settings.embedding_api_key,
                    settings.embedding_model,
                ).result(timeout=settings.health_probe_timeout)
                return DependencyHealth(
                    name=name, status=DependencyStatus.OK, latency_ms=latency,
                    detail=f"{settings.embedding_model}@{settings.embedding_base_url}",
                    checked_at=_now_iso(),
                )
            except FutureTimeout:
                return DependencyHealth(
                    name=name, status=DependencyStatus.DOWN,
                    detail=f"timeout >{settings.health_probe_timeout}s", checked_at=_now_iso(),
                )
            except Exception as exc:
                return DependencyHealth(
                    name=name, status=DependencyStatus.DOWN, detail=_short(exc), checked_at=_now_iso()
                )
        # OLLAMA（原有逻辑不变）
        try:
            latency = self._executor.submit(
                self._ollama_ping, settings.embedding_base_url, settings.embedding_model
            ).result(timeout=settings.health_probe_timeout)
            return DependencyHealth(
                name=name, status=DependencyStatus.OK, latency_ms=latency,
                detail=f"{settings.embedding_model}@{settings.embedding_base_url}",
                checked_at=_now_iso(),
            )
        except FutureTimeout:
            return DependencyHealth(
                name=name, status=DependencyStatus.DOWN,
                detail=f"timeout >{settings.health_probe_timeout}s", checked_at=_now_iso(),
            )
        except Exception as exc:
            return DependencyHealth(name=name, status=DependencyStatus.DOWN, detail=_short(exc), checked_at=_now_iso())
```

在类中新增静态方法（放在 `_ollama_ping` 之后）：

```python
    @staticmethod
    def _openai_compatible_embed_ping(base_url: str, api_key: str, model: str) -> float:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url)
        t0 = time.perf_counter()
        client.embeddings.create(model=model, input="ping")
        return round((time.perf_counter() - t0) * 1000, 1)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_health_embedding.py -v`
Expected: PASS

- [ ] **Step 5: Run existing health/supervisor regression**

Run: `cd backend && python -m pytest tests/test_supervisor.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/src/porto_chatbot/health.py backend/tests/test_health_embedding.py
git commit -m "feat(health): add OPENAI_COMPATIBLE embedding probe"
```

---

### Task 9: Frontend — Types + API Defaults

**Files:**
- Modify: `frontend/src/lib/types.ts`
- Modify: `frontend/src/lib/api.ts`

**Interfaces:**
- Produces: `RagConfig` 新字段类型、`defaultRagConfig` 新字段默认值

- [ ] **Step 1: Update types.ts RagConfig**

在 `frontend/src/lib/types.ts` 的 `RagConfig` type 中：

将 `embedding_provider` 行改为：
```typescript
  embedding_provider: "local" | "ollama" | "openai_compatible";
```

在 `embedding_base_url` 后新增：
```typescript
  embedding_api_key: string | null;
```

在 `rerank_enabled` 前新增：
```typescript
  rerank_type: "llm" | "cross_encoder";
```

在 `rerank_choice_batch_size` 后新增：
```typescript
  rerank_api_key: string | null;
  rerank_base_url: string | null;
```

- [ ] **Step 2: Update api.ts defaultRagConfig**

在 `frontend/src/lib/api.ts` 的 `defaultRagConfig` 对象中，在 `embedding_base_url` 后新增：
```typescript
  embedding_api_key: null,
```

在 `rerank_enabled` 前新增：
```typescript
  rerank_type: "llm",
```

在 `rerank_choice_batch_size` 后新增：
```typescript
  rerank_api_key: null,
  rerank_base_url: null,
```

- [ ] **Step 3: Verify frontend compiles**

Run: `cd frontend && npx tsc --noEmit 2>&1 | head -20`
Expected: 无类型错误（或仅已有无关错误）

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/types.ts frontend/src/lib/api.ts
git commit -m "feat(frontend): add new RAG config types and defaults for embedding/reranking expansion"
```

---

### Task 10: Frontend — Settings UI（Embedding + Rerank）

**Files:**
- Modify: `frontend/src/components/porto-workbench.tsx`

**Interfaces:**
- Consumes: updated `RagConfig` type, `defaultRagConfig`

- [ ] **Step 1: Add openai_compatible option to embedding provider dropdown**

在 `porto-workbench.tsx` 的 RAG Settings 表单（约 L1738），embedding provider `<select>` 中追加：

```tsx
            <option value="openai_compatible">openai_compatible（OpenAI / Jina / Voyage / Fireworks 等）</option>
```

- [ ] **Step 2: Add conditional API Key input for openai_compatible embedding**

在 embedding Base URL 的 `<label>` 之后（约 L1761），追加条件渲染的 API Key 输入：

```tsx
        {ragDraft.embedding_provider === "openai_compatible" ? (
          <label className="block md:col-span-2">
            <span className="text-xs text-zinc-500">Embedding API Key</span>
            <input
              className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
              type="password"
              placeholder="sk-..."
              value={ragDraft.embedding_api_key ?? ""}
              onChange={(event) =>
                updateRag("embedding_api_key", event.target.value || null)
              }
            />
          </label>
        ) : null}
```

- [ ] **Step 3: Update RerankConfig type and rerank form**

在 `RerankConfig` type（约 L2612）的 Pick 中新增字段：

```typescript
type RerankConfig = Pick<
  RagConfig,
  | "rerank_enabled"
  | "rerank_top_n"
  | "rerank_choice_batch_size"
  | "rerank_provider"
  | "rerank_model"
  | "rerank_type"
  | "rerank_api_key"
  | "rerank_base_url"
>;
```

在 `rerankDraft` 初始化（约 L2647）中新增：

```typescript
  const [rerankDraft, setRerankDraft] = useState<RerankConfig>(() => ({
    rerank_enabled: ragConfig.rerank_enabled,
    rerank_top_n: ragConfig.rerank_top_n,
    rerank_choice_batch_size: ragConfig.rerank_choice_batch_size,
    rerank_provider: ragConfig.rerank_provider,
    rerank_model: ragConfig.rerank_model,
    rerank_type: ragConfig.rerank_type,
    rerank_api_key: ragConfig.rerank_api_key,
    rerank_base_url: ragConfig.rerank_base_url,
  }));
```

- [ ] **Step 4: Add Rerank Type selector + conditional fields**

在重排序表单（约 L2819，"启用重排序" checkbox 之后），追加 rerank type 选择器和条件字段。将现有的 rerank provider/model/batch_size 字段用 `{rerankDraft.rerank_type === "llm" ? (...) : null}` 包裹，并新增 cross_encoder 的字段：

在 "启用重排序" 的 `<p>` 描述之后，`<div className="grid gap-4 md:grid-cols-2">` 之前，插入 rerank type 选择器：

```tsx
          <label className="block">
            <span className="text-xs text-zinc-500">重排序类型</span>
            <select
              className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
              disabled={!rerankDraft.rerank_enabled}
              value={rerankDraft.rerank_type}
              onChange={(event) =>
                updateRerank(
                  "rerank_type",
                  event.target.value as RerankConfig["rerank_type"],
                )
              }
            >
              <option value="llm">LLM 提示重排（现有）</option>
              <option value="cross_encoder">Cross-Encoder 专用 Reranker（Jina / Cohere / Voyage）</option>
            </select>
          </label>
```

然后将现有的 Provider / Model / choice_batch_size 字段块用条件包裹：

```tsx
          <div className="grid gap-4 md:grid-cols-2">
            <label className="block">
              <span className="text-xs text-zinc-500">重排序保留数量（Top N）</span>
              <input
                className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
                type="number"
                min={1}
                disabled={!rerankDraft.rerank_enabled}
                value={rerankDraft.rerank_top_n}
                onChange={(event) =>
                  updateRerank("rerank_top_n", Number(event.target.value))
                }
              />
            </label>

            {rerankDraft.rerank_type === "llm" ? (
              <>
                <label className="block">
                  <span className="text-xs text-zinc-500">重排序批大小（choice_batch_size）</span>
                  <input
                    className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
                    type="number"
                    min={1}
                    disabled={!rerankDraft.rerank_enabled}
                    value={rerankDraft.rerank_choice_batch_size}
                    onChange={(event) =>
                      updateRerank("rerank_choice_batch_size", Number(event.target.value))
                    }
                  />
                </label>
                <label className="block">
                  <span className="text-xs text-zinc-500">
                    重排序 Provider（留空复用 Agent）
                  </span>
                  <select
                    className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
                    disabled={!rerankDraft.rerank_enabled}
                    value={rerankDraft.rerank_provider ?? ""}
                    onChange={(event) =>
                      updateRerank(
                        "rerank_provider",
                        (event.target.value || null) as RerankConfig["rerank_provider"],
                      )
                    }
                  >
                    <option value="">（复用 Agent 设置）</option>
                    <option value="openai">openai</option>
                    <option value="anthropic">anthropic</option>
                  </select>
                </label>
                <label className="block">
                  <span className="text-xs text-zinc-500">
                    重排序 Model（留空复用 Agent）
                  </span>
                  <input
                    className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
                    disabled={!rerankDraft.rerank_enabled}
                    placeholder="可选"
                    value={rerankDraft.rerank_model ?? ""}
                    onChange={(event) =>
                      updateRerank("rerank_model", event.target.value || null)
                    }
                  />
                </label>
              </>
            ) : (
              <>
                <label className="block">
                  <span className="text-xs text-zinc-500">Reranker Model</span>
                  <input
                    className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
                    disabled={!rerankDraft.rerank_enabled}
                    placeholder="jina-reranker-v2-base-multilingual"
                    value={rerankDraft.rerank_model ?? ""}
                    onChange={(event) =>
                      updateRerank("rerank_model", event.target.value || null)
                    }
                  />
                </label>
                <label className="block md:col-span-2">
                  <span className="text-xs text-zinc-500">Reranker Base URL</span>
                  <input
                    className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
                    disabled={!rerankDraft.rerank_enabled}
                    placeholder="https://api.jina.ai/v1"
                    value={rerankDraft.rerank_base_url ?? ""}
                    onChange={(event) =>
                      updateRerank("rerank_base_url", event.target.value || null)
                    }
                  />
                </label>
                <label className="block md:col-span-2">
                  <span className="text-xs text-zinc-500">Reranker API Key</span>
                  <input
                    className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
                    type="password"
                    disabled={!rerankDraft.rerank_enabled}
                    placeholder="jina-..."
                    value={rerankDraft.rerank_api_key ?? ""}
                    onChange={(event) =>
                      updateRerank("rerank_api_key", event.target.value || null)
                    }
                  />
                </label>
              </>
            )}
          </div>
```

- [ ] **Step 5: Verify frontend compiles**

Run: `cd frontend && npx tsc --noEmit 2>&1 | head -20`
Expected: 无类型错误

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/porto-workbench.tsx
git commit -m "feat(frontend): add embedding API key + rerank type selector with conditional fields"
```

---

### Task 11: Full Regression + Architecture Diagram Update

**Files:**
- Test: all backend tests
- Modify: `frontend/src/components/architecture-view.tsx`（可选：更新 mermaid 图）

- [ ] **Step 1: Run full backend test suite**

Run: `cd backend && python -m pytest --tb=short -q`
Expected: 全部 PASS

- [ ] **Step 2: Run frontend build**

Run: `cd frontend && npx next build 2>&1 | tail -20`
Expected: BUILD SUCCESS

- [ ] **Step 3: Update architecture mermaid diagram (optional)**

在 `frontend/src/components/architecture-view.tsx` 中，更新检索优化管线图，将 `LLM Rerank` 节点改为 `Rerank (LLM / Cross-Encoder)`。

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "test: full regression pass + architecture diagram update"
```
