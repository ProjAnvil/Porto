# Embedding & Reranking 模型扩展设计

**日期**: 2026-08-08
**状态**: 设计完成，待实现

## 背景与动机

Porto 当前 embedding 仅支持 LOCAL（hash 散列）和 OLLAMA 两个 provider，reranking 仅有 LLMRerank（LLM 提示重排）。这严重限制了 RAG 检索质量：

- **Embedding**：无法接入 OpenAI、Jina AI、Voyage AI、Cohere、Fireworks、HuggingFace TEI、vLLM 等主流 embedding API
- **Reranking**：LLMRerank 慢且贵（每 batch 一次 LLM 调用），缺少专用 cross-encoder reranker（Jina Reranker / Cohere Rerank / Voyage Rerank / BGE-reranker）

研究表明 LangChain 和 LlamaIndex 均完全支持自定义 embedding/reranking 接入，Porto 无需更换框架。

## 设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| Embedding provider 策略 | 单一 `OPENAI_COMPATIBLE` catch-all | 一个 provider 覆盖 OpenAI/Jina/Fireworks/TEI/vLLM 等 5+ 家，用户只需改 base_url + model + api_key |
| Reranking 架构 | 新增 `rerank_type` 枚举，用户选择 | cross-encoder 与 LLMRerank 共存，向后兼容 |
| API Key 管理 | 各加专用字段 | embedding_api_key + rerank_api_key 独立，不混用 agent_api_key |
| 改动范围 | 后端 + 前端一起改 | 开箱即用，用户可在 UI 配置 |
| 代码结构 | Strategy Pattern + Registry | 避免 if-else 链，新增 provider 只加一个类 + 注册表一行 |

## 架构概览

```
用户选择 Embedding Provider
├── LOCAL              → LocalEmbeddingBackend
├── OLLAMA             → OllamaEmbeddingBackend
└── OPENAI_COMPATIBLE  → OpenAICompatibleEmbeddingBackend   ← 新增

用户选择 Rerank Type
├── LLM                → LLMReranker（现有逻辑提取）
└── CROSS_ENCODER      → CrossEncoderReranker               ← 新增
```

### 设计原则

- **fail-open 不变**：新 provider 出错时降级返回原始 chunks（与现有 rerank 行为一致）
- **向后兼容**：默认值保持现有行为（`rerank_type` 默认 `"llm"`，新字段默认 `None`）
- **开闭原则**：新增 provider 只需创建一个类 + 注册表加一行，不修改现有代码
- **改动收敛**：所有 dispatch 逻辑在 `EmbeddingClient` 和 `rerank_chunks()` 内部完成，调用方零改动

## 详细设计

### 1. Enums（`models/enums.py`）

```python
class EmbeddingProvider(StrEnum):
    LOCAL = "local"
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"   # 新增

class RerankType(StrEnum):                     # 新增枚举
    LLM = "llm"               # 现有 LLMRerank
    CROSS_ENCODER = "cross_encoder"  # 专用 cross-encoder API
```

### 2. Settings 新增字段（`settings.py`）

| 字段 | 类型 | 默认值 | 用途 |
|------|------|--------|------|
| `embedding_api_key` | `str \| None` | `None` | `OPENAI_COMPATIBLE` 的 API key |
| `rerank_type` | `RerankType` | `RerankType.LLM` | 切换 reranker 类型 |
| `rerank_api_key` | `str \| None` | `None` | cross-encoder 的 API key |
| `rerank_base_url` | `str \| None` | `None` | cross-encoder 的 API base（如 `https://api.jina.ai/v1`） |

字段复用关系：

- `rerank_model`：LLM type 时填 LLM 模型名，cross-encoder type 时填 reranker 模型名
- `rerank_top_n`：两种 type 共用
- `rerank_provider` / `rerank_choice_batch_size`：仅 LLM type 使用

### 3. Payload 扩展（`models/payload.py`）

`RagSettingsPayload` 新增四个可选字段：

```python
class RagSettingsPayload(BaseModel):
    # ...existing...
    embedding_api_key: str | None = None       # 新增
    rerank_type: RerankType | None = None      # 新增
    rerank_api_key: str | None = None          # 新增
    rerank_base_url: str | None = None         # 新增
```

### 4. Embedding 后端重构（`embeddings.py`）

从 if-else 链重构为 Strategy Pattern + Registry：

```python
class EmbeddingBackend(Protocol):
    """Embedding 后端接口。"""
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...


class LocalEmbeddingBackend:
    """现有 LOCAL hash 散列实现。"""
    def __init__(self, settings: Settings):
        self._dimensions = settings.embedding_dimensions
    def embed_documents(self, texts):
        return [local_embed_text(t, self._dimensions) for t in texts]


class OllamaEmbeddingBackend:
    """现有 OLLAMA 实现。"""
    def __init__(self, settings: Settings):
        self._client = ollama.Client(host=settings.embedding_base_url)
        self._model = settings.embedding_model
    def embed_documents(self, texts):
        resp = self._client.embed(model=self._model, input=list(texts))
        return [list(v) for v in resp["embeddings"]]


class OpenAICompatibleEmbeddingBackend:
    """新增：OpenAI-compatible embedding API。
    覆盖 OpenAI / Jina / Voyage(OpenAI模式) / Fireworks / TEI / vLLM。
    """
    def __init__(self, settings: Settings):
        from openai import OpenAI
        self._client = OpenAI(
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
        )
        self._model = settings.embedding_model
    def embed_documents(self, texts):
        resp = self._client.embeddings.create(model=self._model, input=list(texts))
        return [d.embedding for d in resp.data]


EMBEDDING_BACKENDS: dict[EmbeddingProvider, type] = {
    EmbeddingProvider.LOCAL: LocalEmbeddingBackend,
    EmbeddingProvider.OLLAMA: OllamaEmbeddingBackend,
    EmbeddingProvider.OPENAI_COMPATIBLE: OpenAICompatibleEmbeddingBackend,
}


class EmbeddingClient:
    """Facade：按配置选择后端，委托调用。日志/异常处理在此层。"""
    def __init__(self, settings: Settings):
        self.logger = get_component_logger("embeddings", settings)
        backend_cls = EMBEDDING_BACKENDS.get(settings.embedding_provider)
        if backend_cls is None:
            raise ValueError(f"Unsupported embedding provider: {settings.embedding_provider}")
        self._backend = backend_cls(settings)

    def embed_documents(self, texts):
        self.logger.info("embed documents provider=%s model=%s count=%s", ...)
        return self._backend.embed_documents(texts)

    def embed_query(self, text):
        self.logger.info("embed query provider=%s chars=%s", ...)
        return self._backend.embed_documents([text])[0]
```

- 直接复用已安装的 `openai` SDK，零新依赖
- batch 调用：openai SDK 原生支持 `input=list[str]`，复用现有 `_EMBED_BATCH_SIZE`
- `embed_query()` 无需改——调用 `embed_documents([text])[0]`，自动走新分支
- 新增 provider 只需：创建一个 backend 类 + 注册表加一行

### 5. Reranker 后端（新文件 `rerankers.py`）

从 `retrieval.py` 提取 reranker 逻辑，独立成文件：

```python
class RerankerBackend(Protocol):
    """Reranker 后端接口。"""
    def rerank(self, chunks: list[SourceChunk], query: str) -> list[SourceChunk]: ...


class LLMReranker:
    """现有 LLMRerank 逻辑，零行为变化。
    从 retrieval.py 的 rerank_chunks() + _build_rerank_llm() 提取。
    """
    def __init__(self, settings: Settings):
        self._llm = _build_rerank_llm(settings)  # 复用现有 _build_rerank_llm
        self._top_n = settings.rerank_top_n
        self._batch_size = settings.rerank_choice_batch_size
    def rerank(self, chunks, query):
        # 原 rerank_chunks 的 LLMRerank 逻辑
        ...


class CrossEncoderReranker:
    """新增：POST {base_url}/rerank 专用 cross-encoder。
    兼容 Jina / Cohere / Voyage 的 /v1/rerank 协议。
    """
    def __init__(self, settings: Settings):
        self._base_url = settings.rerank_base_url
        self._api_key = settings.rerank_api_key
        self._model = settings.rerank_model
        self._top_n = settings.rerank_top_n
    def rerank(self, chunks, query):
        resp = httpx.post(
            f"{self._base_url}/rerank",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "query": query,
                "documents": [{"text": c.text} for c in chunks],
                "top_n": min(self._top_n, len(chunks)),
            },
            timeout=settings.agent_request_timeout,
        )
        data = resp.json()
        results = data.get("results") or data.get("data") or []
        # 按 relevance_score 降序，映射回原 SourceChunk
        ...


RERANKER_BACKENDS: dict[RerankType, type] = {
    RerankType.LLM: LLMReranker,
    RerankType.CROSS_ENCODER: CrossEncoderReranker,
}
```

**协议兼容策略：**

| 差异点 | Jina | Cohere | Voyage | 处理方式 |
|--------|------|--------|--------|----------|
| documents 格式 | `[{text}]` 或 `[str]` | `[{text}]` | `[str]` | 发 `[{text}]`（Jina+Cohere 兼容）；Voyage 按 host 判断发 `[str]` |
| 响应 key | `results` | `results` | `data` | `results or data` 兼容两者 |
| score 字段 | `relevance_score` | `relevance_score` | `relevance_score` | 统一 |

### 6. retrieval.py 调用方改动

```python
from .rerankers import RERANKER_BACKENDS

def rerank_chunks(chunks, query, settings):
    if not settings.rerank_enabled or not chunks:
        return chunks
    backend_cls = RERANKER_BACKENDS.get(settings.rerank_type)
    if backend_cls is None:
        return chunks
    try:
        backend = backend_cls(settings)
        return backend.rerank(chunks, query)
    except Exception:
        logger.exception("rerank failed ...")
        return chunks  # fail-open
```

### 7. Health 探测扩展（`health.py`）

`_probe_embedding()` 新增 OPENAI_COMPATIBLE 探测：

```python
def _probe_embedding(self, settings):
    provider = settings.embedding_provider
    if provider == EmbeddingProvider.LOCAL:
        return DependencyHealth(status=OK, detail="local")
    if provider == EmbeddingProvider.OLLAMA:
        return self._ollama_ping_probe(settings)       # 不变
    if provider == EmbeddingProvider.OPENAI_COMPATIBLE:
        return self._openai_compatible_ping_probe(settings)  # 新增
```

新增 `_openai_compatible_ping_probe()`：调用 `client.embeddings.create(input="ping")` 测连通性。

### 8. 前端改动

#### `types.ts`

```typescript
export type RagConfig = {
  embedding_provider: "local" | "ollama" | "openai_compatible";  // +openai_compatible
  embedding_model: string;
  embedding_base_url: string;
  embedding_api_key: string | null;           // 新增
  // ...existing...
  rerank_type: "llm" | "cross_encoder";       // 新增
  rerank_enabled: boolean;
  rerank_top_n: number;
  rerank_provider: "openai" | "anthropic" | null;
  rerank_model: string | null;
  rerank_choice_batch_size: number;
  rerank_api_key: string | null;              // 新增
  rerank_base_url: string | null;             // 新增
};
```

#### `porto-workbench.tsx`

两处 UI 改动：

**RAG Settings 表单**（embedding 部分）：
- Provider 下拉新增 `<option value="openai_compatible">`
- 当选择 `openai_compatible` 时，显示 API Key 输入框（`embedding_api_key`）

**重排序表单**：
- 新增 Rerank Type 选择器（LLM / Cross-Encoder 单选卡片）
- `RerankConfig` Pick 类型扩展新字段
- 条件渲染：
  - LLM（默认）：显示 Provider / Model / choice_batch_size
  - Cross-Encoder：显示 Model / Base URL / API Key

#### `api.ts`

`defaultRagConfig` 新增字段默认值（与后端 Settings 对齐）。

## 错误处理

| 场景 | 失败行为 | 理由 |
|------|----------|------|
| Embedding API 调用失败 | `raise`（向上传播） | embedding 失败时无法继续索引/检索，需要让上层感知 |
| Cross-encoder rerank API 失败 | 返回原始 chunks | rerank 只是优化步骤，不应阻断检索 |
| Cross-encoder 构造失败（缺 key/url） | 返回原始 chunks | fail-open |
| Health 探测失败 | `DependencyStatus.DOWN` | 前端显示降级状态 |

## 测试策略

| 测试文件 | 覆盖内容 |
|----------|----------|
| `test_embeddings.py`（新建） | 三个 backend 的 `embed_documents()`；`EmbeddingClient` 注册表 dispatch；mock openai SDK |
| `test_rerankers.py`（新建） | `LLMReranker` 行为不变回归；`CrossEncoderReranker` mock httpx 调用；fail-open 路径；`RERANKER_BACKENDS` dispatch |
| `test_settings_fields.py`（扩展） | 新字段默认值；payload 序列化/反序列化 |
| `test_settings_api.py`（扩展） | `RagSettingsPayload` 新字段的 GET/PUT 往返 |
| `test_vector_store.py`（扩展） | `OPENAI_COMPATIBLE` embedding 在 build/search 中的集成 |
| `test_health.py`（新建/扩展） | `_probe_embedding` 对 OPENAI_COMPATIBLE 的探测逻辑 |

Mock 策略：
- OpenAI SDK：mock `openai.OpenAI` 类的 `embeddings.create()` 返回
- httpx：mock `httpx.post()` 返回 rerank 响应
- 不依赖真实 API key

## 向后兼容

- `embedding_provider` 默认 `LOCAL`：现有用户零感知
- `rerank_type` 默认 `LLM`：现有 rerank 行为零变化
- `rerank_enabled` 默认 `False`：现有用户零感知
- 新字段默认 `None`：Pydantic 序列化忽略
- Chroma collection metadata 检查不受影响（`COLLECTION_METADATA_KEYS` 不含新字段）

## 改动清单

**后端 7 文件：**
1. `models/enums.py` — +`EmbeddingProvider.OPENAI_COMPATIBLE`、+`RerankType` 枚举
2. `settings.py` — +4 个新字段
3. `models/payload.py` — +4 个新字段
4. `embeddings.py` — 重构为 strategy + registry
5. `rerankers.py`（新建）— 从 retrieval.py 提取 + 新增 CrossEncoderReranker
6. `retrieval.py` — `rerank_chunks()` 改为 registry dispatch
7. `health.py` — embedding 探测新增 OPENAI_COMPATIBLE 分支

**前端 3 文件：**
8. `lib/types.ts` — 类型扩展
9. `lib/api.ts` — 默认值
10. `components/porto-workbench.tsx` — UI 新增选项 + 条件字段

**测试 4-6 文件：**
11-16. 新建/扩展测试文件
