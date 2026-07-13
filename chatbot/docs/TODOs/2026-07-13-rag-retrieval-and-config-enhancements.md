# RAG 检索与配置增强 · 实现计划

> **执行方式：** inline 逐 task 实现，每 task 一个 commit。设计依据 `docs/PLANs/2026-07-13-rag-retrieval-and-config-enhancements.md`。

**Goal:** 多目录知识库 + bm25s 混合检索 + critic 独立卡片 + 保存/Re-index 分离 + sources 显示路径。

**Tech Stack:** Python 3.14 / FastAPI / chromadb / bm25s / Pydantic / React+TS。

## Global Constraints
- BM25 库固定 `bm25s>=0.2.0`（pyproject）。
- BM25 分词复用 `embeddings.tokens()`，**不**引入 jieba。
- BM25 索引落 `~/.porto/bm25s_index/`（目录，bm25s 原生 save/load）+ sidecar `ids.json`/`metadatas.json`。
- source path 统一 `{root.name}/{rel}`；chunk id 串纳入此前缀防撞。
- retrieval 默认 `hybrid`，RRF k=60；不暴露权重旋钮。
- 后端测试用 `sample_settings`（embedding_provider=local），从 `chatbot/backend` 跑 `uv run pytest`；前端 `npx tsc --noEmit`。
- commit 风格：`feat(chatbot): ...` / `test(chatbot): ...` / `refactor(chatbot): ...`，结尾 `Co-Authored-By: Claude <noreply@anthropic.com>`。

---

## Task 1: 加 bm25s 依赖
**Files:** Modify `chatbot/backend/pyproject.toml`
- [ ] 在 dependencies 加 `"bm25s>=0.2.0"`；`uv lock` + `uv sync`
- [ ] 验证 `uv run python -c "import bm25s; print(bm25s.__version__)"` 成功
- [ ] commit `chore(chatbot): 加 bm25s 依赖`

## Task 2: Bm25Index（bm25s 包装）
**Files:** Create `chatbot/backend/src/porto_chatbot/bm25_index.py`; Test `chatbot/backend/tests/test_bm25_index.py`
**Produces:**
```python
@dataclass
class ChunkRecord: chroma_id: str; text: str; metadata: dict

class Bm25Index:
    def build(self, chunks: list[ChunkRecord]) -> None
    def query(self, text: str, top_k: int) -> list[tuple[str, dict, float]]  # (chroma_id, metadata, score)
    def save(self, dir: Path) -> None   # bm25s retriever.save(dir, corpus=texts) + 写 ids.json/metadatas.json
    @classmethod
    def load(cls, dir: Path) -> "Bm25Index"  # bm25s.BM25.load(dir, load_corpus=True, mmap=True) + 读 sidecar
```
- 分词：`" ".join(embeddings.tokens(text))` → `bm25s.tokenize(..., stemmer=None, stopwords=None)`；`bm25s.BM25(method="lucene")`；retrieve 返回 doc 位置索引 → 用 self._ids/self._metadatas 映射。
- [ ] 写测试：build 小语料 → query 关键词返回正确 chroma_id 排序；save→load 往返 query 结果一致
- [ ] 实现 + `uv run pytest tests/test_bm25_index.py -v` 通过
- [ ] commit `feat(chatbot): Bm25Index（bm25s 包装 + 持久化）`

## Task 3: Bm25Registry（进程级单例）
**Files:** Modify `chatbot/backend/src/porto_chatbot/bm25_index.py`; Test `chatbot/backend/tests/test_bm25_index.py`
**Produces:**
```python
class Bm25Registry:
    @classmethod
    def get(cls, settings: Settings) -> Bm25Index | None   # 按 data_dir 缓存，懒加载 ~/.porto/bm25s_index/
    @classmethod
    def build_and_save(cls, settings: Settings, chunks: list[ChunkRecord]) -> Bm25Index
    @classmethod
    def invalidate(cls, data_dir: Path) -> None
```
- 缓存 key = `str(data_dir)`；dir 不存在时 get 返回 None。
- [ ] 测试：build_and_save 后 get 命中；invalidate 后 get 重新加载
- [ ] 实现 + 测试通过；commit `feat(chatbot): Bm25Registry 进程级缓存`

## Task 4: Settings kb_dirs + retrieval 配置
**Files:** Modify `chatbot/backend/src/porto_chatbot/settings.py`
**Produces:** `kb_dirs: list[Path]`（默认 `[Path.home()/".scv"/"analysis"]`）；`@property kb_path -> kb_dirs[0]`；`retrieval_method: Literal["vector","bm25","hybrid"] = "hybrid"`；`bm25_top_k: int = Field(default=20, ge=1)`。
- [ ] 删除原 `kb_path` 字段，加 `kb_dirs` + property；注意 `expand_path` validator 改为对 list 每项展开。
- [ ] `uv run pytest -q` 现有用例不回归（kb_path property 兼容）
- [ ] commit `refactor(chatbot): Settings kb_path→kb_dirs + retrieval 配置`

## Task 5: iter_documents 多目录
**Files:** Modify `chatbot/backend/src/porto_chatbot/documents.py`; Test `chatbot/backend/tests/test_documents.py`
**Produces:** `iter_documents(roots: list[Path]) -> list[tuple[Path, Path]]`（返回 `(root, file)`，按文件绝对路径去重）。
- [ ] 测试：两个目录各放文件 → 返回成对 (root,file)；同名文件不丢
- [ ] 实现；commit `feat(chatbot): iter_documents 支持多目录`

## Task 6: ConfigStore rag kb_dirs/retrieval + apply_rag_settings + payload
**Files:** Modify `chatbot/backend/src/porto_chatbot/config_store.py`（RAG_SETTING_KEYS 加 `kb_dirs`/`retrieval_method`/`bm25_top_k`）、`models/payload.py`（`RagSettingsPayload` 加三字段）、`api/deps.py`（`apply_rag_settings` 把 `kb_dirs: list[str]` 转 `list[Path]`、`default_rag_settings`/`effective_rag_settings` 带新字段）
- [ ] `uv run pytest tests/test_config_store.py -q` 通过；加 kb_dirs 往返测试
- [ ] commit `feat(chatbot): RAG 配置支持多目录与检索算法`

## Task 7: vector_store.build 多目录 + chunk id 防冲突 + source path
**Files:** Modify `chatbot/backend/src/porto_chatbot/vector_store.py`（`_build_impl` 用 `iter_documents(settings.kb_dirs)`、每文件算 `root_name/rel` 作 path、id 串用完整 path）；Test `chatbot/backend/tests/test_vector_store.py`
- [ ] 测试：两目录同名文件 → 两个 chunk 都保留（count=2），metadata.path 含 root.name 前缀
- [ ] 实现 + `uv run pytest tests/test_vector_store.py -q`；commit `feat(chatbot): build 支持多目录 + chunk id 防冲突`

## Task 8: vector_store.search hybrid RRF + is_rag_ready
**Files:** Modify `chatbot/backend/src/porto_chatbot/vector_store.py`（`search` 按 `retrieval_method` 分支；hybrid 取 vector+bm25 各 `bm25_top_k` → RRF k=60 → top_k；`is_rag_ready` 纳入 BM25 一致性）；Test `chatbot/backend/tests/test_vector_store.py`
**Consumes:** `Bm25Registry.get(settings)`；`embeddings.tokens`（Bm25Index 内部用）
- [ ] 测试：构造索引，`hybrid` 命中关键词优于纯 vector；`bm25` only 与 `vector` only 各自可用；bm25 缺失 + method=hybrid → is_rag_ready False
- [ ] 实现 + 测试；commit `feat(chatbot): search 支持 hybrid RRF 融合`

## Task 9: IndexSupervisor 集成 BM25 构建
**Files:** Modify `chatbot/backend/src/porto_chatbot/index_supervisor.py`（`_execute` 在 `store.build()` 后 `collection.get(include=["documents","metadatas"])` 收集成 `ChunkRecord` → `Bm25Registry.build_and_save`）
- [ ] 扩展 `tests/test_supervisor.py`：submit 完成后 `~/.porto/bm25s_index/` 存在，`Bm25Registry.get` 非空
- [ ] `uv run pytest tests/test_supervisor.py -q`；commit `feat(chatbot): reindex 同步构建 BM25 索引`

## Task 10: 前端 types.ts RagConfig 新字段
**Files:** Modify `chatbot/frontend/src/lib/types.ts`
- [ ] `RagConfig` 加 `kb_dirs: string[]`、`retrieval_method: "vector"|"bm25"|"hybrid"`、`bm25_top_k: number`；`defaultRagConfig`（api.ts）同步默认值
- [ ] `npx tsc --noEmit` 通过；commit `feat(chatbot): RagConfig 新增 kb_dirs/retrieval 字段`

## Task 11: 前端 ChunkList 显示 path
**Files:** Modify `chatbot/frontend/src/components/porto-workbench.tsx:1622`（ChunkList 主标题改 `chunk.path`，副标题 `chunk.title`）
- [ ] tsc 通过；commit `feat(chatbot): sources 显示目录相对路径`

## Task 12: 前端 critic 独立卡片
**Files:** Modify `chatbot/frontend/src/components/porto-workbench.tsx`（AgentSettingsForm：critic 字段移出 `<details>高级配置`，做成独立卡片，复用主 agent generator 控件；高级配置只留 Spec/Workflow/Memory/Context）
- [ ] tsc 通过；commit `refactor(chatbot): critic 提升为独立 agent 卡片`

## Task 13: 前端 保存/Re-index 分离
**Files:** Modify `chatbot/frontend/src/components/porto-workbench.tsx`（RagSettingsForm 底部两个按钮：保存配置=只 saveAppSettings、Re-index=refreshIndex；Knowledge 面板 Re-index 保留）
- [ ] tsc 通过；commit `feat(chatbot): RAG 保存与 Re-index 分离`

## Task 14: 前端 多目录 UI + retrieval 表单
**Files:** Modify `chatbot/frontend/src/components/porto-workbench.tsx`（Knowledge 面板加 kb_dirs 列表+增删；RagSettingsForm 加 retrieval_method 下拉 + bm25_top_k 数字框）
- [ ] tsc 通过；commit `feat(chatbot): 多目录管理 + 检索算法选择 UI`

## Task 15: 端到端验证
- [ ] `cd chatbot/backend && uv run pytest -q` 全绿
- [ ] `cd chatbot/frontend && npx tsc --noEmit` 无错误
- [ ] commit `test(chatbot): 端到端验证（如无新改动则空 commit 跳过）`

## Self-Review
- spec 覆盖：①critic→T12 ②BM25→T2/3/8/9 ③多目录→T4/5/6/7/14 ④sources path→T7/11 ⑤保存分离→T13；retrieval 配置→T4/6/8/10/14。全覆盖。
- 类型一致：`ChunkRecord`（T2 定义，T7/9 消费）；`Bm25Registry.get/build_and_save/invalidate`（T3 定义，T8/9 消费）；`kb_dirs: list[Path]`（T4 定义，T5/7 消费）；`retrieval_method` 字面量三处一致。
- 无占位符。
