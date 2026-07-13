# RAG 检索与配置增强 · 设计文档

- 日期：2026-07-13
- 状态：已与用户逐节确认，待实现
- 上游依赖：2026-07-12 已落地的 IndexSupervisor（单 worker reindex + 持久化 service_locks + HealthMonitor）

## 1. 背景与目标

上一轮已根治 reindex 并发互删（IndexSupervisor 单 worker + search 不再同步 rebuild）。本轮在此基础上增强检索能力与配置体验：

1. **Critic agent 可见化**：critic 配置当前藏在 Agent 页面「高级配置」折叠区，提升为独立卡片，字段控件复用主 agent 的 generator 表单。
2. **BM25 混合检索**：当前 search 为纯 chroma 向量检索；新增 BM25 稀疏检索，与向量 RRF 融合，提升关键词命中。
3. **多知识库目录**：`kb_path`（单一目录）改为目录列表，前端可增删。
4. **sources 显示路径**：检索结果展示 `{目录名}/{相对路径}`，多目录下可辨识出处。
5. **保存与 Re-index 分离**：RAG 子页面「保存配置」与「Re-index」拆为两个独立动作，当前保存即触发 reindex。

## 2. 后端设计

### 2.1 多目录

- `Settings.kb_path: Path` → **`kb_dirs: list[Path]`**（默认 `[~/.scv/analysis]`）。保留 `kb_path` 作为 `@property` 返回 `kb_dirs[0]`，兼容现有引用。
- `iter_documents(roots: list[Path]) -> list[tuple[Path, Path]]`：返回 `(root, file)`，遍历所有根目录合并去重（按文件路径）。
- `ConfigStore` rag namespace 新增 `kb_dirs`（持久化用户添加的目录列表）。
- **source path 格式**：`{root.name}/{相对 root 的路径}`（如 `analysis/payment-platform.md`）。build 时 `metadata["path"]` 写此值，`metadata["root"]` 写 `root.name`。
- **chunk id 防冲突**：id 生成串纳入 `{root.name}/{rel}` 前缀（现有 `sha1(f"{rel}:{i}:{chunk[:120]}")` 改为基于含 root 前缀的完整 path），避免多目录同名文件 id 撞车导致 chunk 被 chroma 覆盖。
- 重名目录 MVP 不消歧（仅 log warning），由用户自行避免。

### 2.2 BM25 索引（新模块 `bm25_index.py`，基于 bm25s）

- 组件 `Bm25Index`：
  - `build(chunks: list[ChunkRecord])`：chunks 为 `(chroma_id, text, metadata)` 列表，顺序与 build 时写入 chroma 一致。
  - `query(text: str, top_k: int) -> list[(chroma_id, metadata, score)]`。
  - `save(dir: Path)` / `load(dir: Path)`。
- **分词**：复用 `embeddings.tokens()`（CJK 单字 + 双字 bigram，已是 embedding 侧分词器）预分词，拼空格串喂 `bm25s.tokenize(stemmer=None, stopwords=None)`——中文友好、零新分词依赖。
- **bm25s 用法**：
  - `retriever = bm25s.BM25(method="lucene", k1=1.5, b=0.75)`
  - `corpus_tokens = bm25s.tokenize([" ".join(tokens(t)) for t in texts], stemmer=None, stopwords=None)` → `retriever.index(corpus_tokens)`
  - 查询：`results, scores = retriever.retrieve(bm25s.tokenize(" ".join(tokens(q)), ...), k=top_k)`，`results` 为 doc 位置索引 → 通过 sidecar 映射回 chroma_id + metadata。
- **持久化**：bm25s 原生 `retriever.save(dir, corpus=texts)` 落到目录 `~/.porto/bm25s_index/`；sidecar `ids.json` + `metadatas.json` 保存 chroma chunk id 与 metadata（bm25s retrieve 返回位置索引，靠它映射回 `SourceChunk`）。`load` 用 `BM25.load(dir, load_corpus=True, mmap=True)`。
- `Bm25Registry`：进程级单例，按 `data_dir` 缓存已加载的 `Bm25Index`。`get(settings)` 懒加载；`build_and_save(settings, chunks)` 重建并落盘 + invalidate 内存；`invalidate(data_dir)`。

### 2.3 构建时机

`IndexSupervisor._execute` 在 `store.build()` 完成后，从 chroma collection 读全量 `(ids, documents, metadatas)`（`collection.get(include=["documents","metadatas"])`），调 `Bm25Registry.build_and_save(settings, chunks)`。BM25 与 chroma collection 同源、同序、同生命周期（都随 reindex 重建），无需改 `vector_store.build` 签名。

### 2.4 检索融合（`vector_store.search` 改造）

- 新配置（Settings + `RagSettingsPayload` + ConfigStore rag namespace）：
  - `retrieval_method: Literal["vector","bm25","hybrid"]`，默认 `"hybrid"`
  - `bm25_top_k: int = 20`（hybrid 时向量与 bm25 各取的候选数）
- search 按 `retrieval_method` 执行：
  - `vector`：现有 chroma 路径（取 `top_k`）。
  - `bm25`：`Bm25Registry.get(settings).query(query, top_k)` → 直接构造 `SourceChunk`（text 从 bm25s corpus 取）。
  - `hybrid`：vector 取 `bm25_top_k` 候选 + bm25 取 `bm25_top_k` 候选 → **RRF 融合** `score(d) = Σ 1/(60 + rank_in_each_list)` → 取最终 `top_k`，text/metadata 从两路中任一取（同 chunk 一致）。
- `is_rag_ready()` 扩展：若 `retrieval_method` 含 bm25，额外要求 `Bm25Registry.get(settings)` 非空且其 chunk 数与 chroma collection.count() 一致，否则判 `index_unavailable`（触发前端提示手动 reindex）。

### 2.5 接口契约变更

- `Settings`：`kb_path: Path` → `kb_dirs: list[Path]`（+ `kb_path` property 兼容）；加 `retrieval_method`、`bm25_top_k`。
- `RagSettingsPayload`（payload.py）：加 `kb_dirs: list[str] | None`、`retrieval_method`、`bm25_top_k`。
- `apply_rag_settings`：把 payload 的 `kb_dirs`（list[str]）转 `list[Path]` 合入 settings。
- `SourceChunk.path`：build 时即写 `{root.name}/{rel}`（前端直接展示）。
- `ConfigStore` rag namespace keys 加 `kb_dirs`、`retrieval_method`、`bm25_top_k`。

### 2.6 依赖

`pyproject.toml` 加 `bm25s>=0.2.0`。

## 3. 前端设计

### 3.1 Critic 独立卡片（`AgentSettingsForm`）

- 把 critic 字段从 `<details>高级配置`（porto-workbench.tsx:1109-1301）提出，做成与主 agent 同级的**独立卡片**「Critic LLM」。
- 字段控件复用主 agent generator 表单：`critic_provider`（下拉 openai/anthropic/继承=null）、`critic_model`、`critic_base_url`、`critic_api_key`、`critic_temperature`、`critic_max_tokens`。
- 主 agent 卡片 + Critic 卡片上下堆叠；「高级配置」折叠区只留 Spec loop / Workflow / Memory / Context。

### 3.2 保存 / Re-index 分离（`RagSettingsForm` + Knowledge 面板）

- RAG 子页面底部两个独立按钮：
  - **「保存配置」**：仅 `saveAppSettings({ rag })`，不 reindex；成功提示「RAG 配置已保存」。
  - **「Re-index」**：触发 `refreshIndex`（异步轮询进度，复用上一轮实现）。
- 改了目录列表或检索参数后，UI 提示「需 Re-index 生效」。

### 3.3 多目录 UI（Knowledge 面板）

- Knowledge 卡片新增目录列表：每行展示 `{root.name}` + 完整路径（只读），带删除按钮；下方输入框 +「添加」追加。
- 目录列表是 RAG 配置的一部分（`kb_dirs`），随「保存配置」持久化。

### 3.4 ChunkList 显示路径（porto-workbench.tsx:1622）

- `chunk.title || chunk.path` → 主标题改为 `chunk.path`（`{root.name}/{相对路径}`），副标题小字显示 `chunk.title`（文件名）；`<details>` 展开后显示完整 `chunk.text`。

### 3.5 新 RAG 配置字段（types.ts `RagConfig`）

加 `kb_dirs: string[]`、`retrieval_method: "vector"|"bm25"|"hybrid"`、`bm25_top_k: number`；RAG 表单加 `retrieval_method` 下拉 + `bm25_top_k` 数字框。

## 4. 测试策略

### 后端（pytest）
- `iter_documents` 多目录合并、去重、`(root, file)` 返回。
- `Bm25Index.build/query/save/load` 往返一致；query 返回正确 chroma_id 映射。
- `vector_store.search` 三种 method 各自行为；hybrid 的 RRF 融合结果合理（构造 vector/bm25 各自排序，验证融合后排序）。
- source path 格式 `{root.name}/{rel}`。
- `is_rag_ready`：bm25 缺失时（method=hybrid）判 unavailable。
- 现有 reindex/supervisor 测试扩展：build 后 `~/.porto/bm25s_index/` 存在；重启后 Bm25Registry.load 恢复。

### 前端
- `tsc --noEmit` 无错误。

## 5. 不做（YAGNI）

- BM25 权重旋钮（hybrid 用固定 RRF k=60，不暴露权重）。
- 重名目录自动消歧（仅 warning）。
- 检索后 cross-encoder/LLM rerank（需求 C，本轮不做）。
- BM25 索引的增量更新（始终随 reindex 全量重建）。
- 多目录的目录级开关（全部参与检索）。
