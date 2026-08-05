# 文件服务统一 + Send Fan-out 拆解 设计

- **日期**: 2026-08-05
- **状态**: 已确认，待写实现计划
- **作者**: yuhaochen + Claude
- **关联**: `docs/superpowers/plans/2026-07-24-langgraph-orchestration-l2.md`（L3 待做项的本设计）

## 1. 背景与动机

Porto 有两个消费用户文件的场景：

- **workflow 场景**：用户上传 PRD 文档 → 拆解成多个 subsystem spec
- **chatbot 场景**：用户在聊天里发文件提问

现状存在三组问题：

1. **文件处理只存在于 workflow 一条路径**（`/api/porto/workflows/upload`），原始字节不落盘（tempfile → 抽文本 → `unlink`），chatbot 聊天面板没有附件能力。
2. **大文档 context 爆炸**：state 里存完整 `prd_text`，虽然 `understand` 节点 LLM 已是 tool-calling 按需读取，但 `retrieve` / fallback / generate 的 SpecContext 仍直接读全文——前端即使分页，后端又"一口吞回去"。
3. **spec 生成用 `ThreadPoolExecutor` 而非声明式图编排**：无法被 LangSmith 追踪子图内部状态，L3（Send map-reduce）长期挂账。

本设计一次性解决以上三点：统一文件服务（pointer 模式）+ chatbot 上传 + L3 Send fan-out。

## 2. 决策汇总

| # | 决策 | 依据 |
|---|---|---|
| 范围 | 两个子系统合一个 spec，端到端"上传→拆解"流水线 | 用户选择 |
| 文件服务 | 统一改造，workflow + chatbot 共用一套 `FileService` | 用户选择 |
| read_file 能力 | 分页按需（`get_file_info` / `read_file_pages` / `search_file`） | pdf-mcp 模式 |
| 文件流转模式 | **Memory Pointer**：state 只存 `prd_file_id`，不存全文 | [arXiv 2511.22729](https://arxiv.org/html/2511.22729v1)、[AWS memory pointer](https://dev.to/aws/ai-context-window-overflow-memory-pointer-fix-3akc) |
| L3 编排 | Send fan-out + spec 子图化（critique-refine 循环） | 替代 ThreadPoolExecutor |
| evaluate 节点 | **删除**，质量由子图内部循环保证，spec 各自交付页面供审计 | 用户选择 |
| 并发 | `spec_refine_concurrency` 默认 3→**4**，给 Send Semaphore 限流；字段名保持不变（族一致） | 用户选择 |
| 架构图 | 更新 `architecture-view.tsx` 的 `LANGGRAPH` 图 + 新增 `FILE_SERVICE` 图 | 用户要求 |

## 3. 整体架构与端到端数据流

```
用户上传文件
   │
   ▼
FileService.store(file, owner_id) ── 落盘 data_dir/files/{file_id}/ ── 元数据入 SQLite
   │
   ├─【workflow 场景】state 存 prd_file_id（pointer），不存全文
   │     LangGraph:  retrieve → understand → identify
   │        │  Send fan-out（并发 = spec_refine_concurrency, 默认 4, Semaphore 限流）
   │        ├─→ spec 子图[A]: init→critique→(refine|finalize) → spec_results["A"]
   │        ├─→ spec 子图[B]: ... → spec_results["B"]
   │        └─→ spec 子图[C]: ... → spec_results["C"]
   │     各 spec 独立进 dict，页面展示供审计 ── END
   │     （❌ 删除 evaluate 节点 + rework 回边）
   │
   └─【chatbot 场景】文件关联 session（owner_id = session_id）
         Agent SDK: Claude 自主调 read_file_pages / search_file / get_file_info 按需读取
```

**核心原则**：`FileService` 是唯一的文件访问层。workflow 节点和 chatbot agent 的 read_file tool handler 都调同一个 `FileService`——这是"统一文件服务"的真正含义。

## 4. FileService 设计（pointer 模式）

**新增** `backend/src/porto_chatbot/files/service.py`：

```python
class FileService:
    def store(self, file: UploadFile, owner_id: str) -> FileMeta
        # 落盘 data_dir/files/{file_id}/{original_name} + 元数据入 SQLite
        # store 时一次性预提取分页：
        #   PDF → pypdf 逐页提取 pages: list[str]（真实页码）
        #   DOCX/MD/TXT → 全文按 ≈2000 字符虚拟分页

    def get_info(self, file_id: str) -> FileInfo
        # {page_count, size_bytes, original_name, mime, created_at}

    def read_pages(self, file_id: str, start: int, end: int) -> str
        # 按页返回文本片段；PDF 用真实页码，其他类型虚拟页

    def search(self, file_id: str, query: str) -> list[Hit]
        # 关键词匹配返回 [{page, snippet}]（先简单字符串匹配，后续可升 FTS）
```

> **没有 `parse()` 全文返回方法**。任何需要文本的地方都通过 `read_pages` / `search` 按需读取。

**存储**：
- **落盘**：`{data_dir}/files/{file_id}/{original_name}`（data_dir 即现有 `~/.porto-chatbot`）
- **元数据**：现有 sqlite 库（与 workflow_store 同库）新增 `files` 表：

```sql
CREATE TABLE files (
  file_id TEXT PRIMARY KEY,
  owner_id TEXT,            -- session_id 或 workflow_id
  original_name TEXT,
  stored_path TEXT,
  mime TEXT,
  size_bytes INTEGER,
  page_count INTEGER,
  pages_json TEXT,          -- 预提取的分页文本（read_pages/search 用，避免每次重解析）
  created_at TEXT
);
```

**分页策略**（解决"一次性吞全文"问题）：
- **PDF**：store 时 `pypdf` 逐页提取 → `pages: list[str]`，`read_pages(5,8)` 返回真实第 5-8 页
- **DOCX/MD/TXT**：store 时提取全文，按 ≈2000 字符虚拟分页

## 5. read_file tool 组

注册到**两个消费者**（同一组 tool，handler 都调 FileService）：

| tool | 入参 | 返回 | 消费者 |
|---|---|---|---|
| `get_file_info` | `file_id` | 页数/大小/名/mime | workflow 节点 + chatbot agent |
| `read_file_pages` | `file_id, start, end` | 该页范围文本 | 同上 |
| `search_file` | `file_id, query` | `[{page, snippet}]` | 同上 |

**workflow 侧**：`agent.backend.build_tools(ctx)` 里现有 `get_prd_text`（`agent_sdk/tools.py:76` → `handlers.py`）→ 演进为上述 read_file 组。

**chatbot 侧**：`create_sdk_mcp_server`（`agent_sdk/backend.py:348`）注册同一组 tool，与现有 `search_knowledgebase` / `search_memory` 并列。

## 6. chatbot 上传（前端 + API + 关联）

| 层 | 改动 | 位置 |
|---|---|---|
| **前端 UI** | 聊天面板加附件按钮（复用 workflow 现有隐藏 input 模式 `porto-workbench.tsx:2366`），支持多文件，已选文件列表显示在输入框上方 | `frontend/src/components/porto-workbench.tsx` 聊天面板 |
| **上传 API** | 新增 `POST /api/chat/files`（FormData）→ `FileService.store(owner_id=session_id)` → 返回 `file_id` | `backend/src/porto_chatbot/api/routes/chat.py` + `frontend/src/lib/api.ts` |
| **消息契约** | `ChatRequest` 加 `file_ids: list[str] = []` | `backend/src/porto_chatbot/models/chat.py` |
| **关联** | `files.owner_id = session_id`；agent 通过 `file_ids` 知道当前消息带哪些文件 | 同上 |

## 7. workflow 改造

### 7.1 state pointer 化（`backend/src/porto_chatbot/agent/agent/state.py`）

```
prd_text: str  ❌  →  prd_file_id: str  ✅（pointer）
# spec_results 保留 _dict_merge reducer（子图结果收集）
# 删除 needs_rework / rework_passes 相关字段
```

### 7.2 节点逐个改造

| 节点 | 现状 | 改造 |
|---|---|---|
| upload（`api/routes/workflow.py:150`） | tempfile → 抽文本 → 删文件 → state.prd_text | `FileService.store` → state.prd_file_id |
| retrieve（`agent/nodes/retrieve.py:8`） | `state['prd_text'][:2000]` | `FileService.read_pages(file_id, 1, N)` 生成 query |
| understand（`agent/nodes/understand.py:26`） | LLM tool-calling `get_prd_text` | tool 换成 read_file 组（handler 调 FileService） |
| understand fallback（`:78`） | 直接读全文启发式 | FileService 读前 N 页片段 |
| identify | 同 understand，tool-calling | 同上 |
| generate（`agent/nodes/generate.py:29`） | `ThreadPoolExecutor` 并行 | **删除** → Send fan-out |
| evaluate（`agent/nodes/evaluate.py`） | 全局评分 + rework | **整节点删除** + 去掉 graph 回边 |

### 7.3 graph 结构（`agent/agent/graph.py:34`）

```
retrieve → understand → identify
   │  conditional_edges: Send("spec_subgraph", {sub, prd_file_id}) 每个 subsystem 一份
   ▼
spec_subgraph ×N（Semaphore 限流，默认 4）── 各 spec_results[sub.name] ── END
```

## 8. spec 子图结构（四重终止）

**新增** `backend/src/porto_chatbot/specs/subgraph.py`，封装现有 `specs/loop.py:16` 的 `generate_spec_with_loop` 逻辑。

```
子图 state: {sub, prd_file_id, current_spec, best_spec, best_score,
             used_chars, attempts: list[SpecAttempt], iteration}

  init_spec(generate_initial_spec) → critique
                                      │
                                四重终止判断 ──┐
                                      │        │
                                ┌─ 终止 ─┐     │ 否
                                │        │     ▼
                              是         │   refine → critique（回边循环）
                                │        │
                                ▼        │
                            finalize     │
                         （选 best_spec）│
                                │        │
                                ▼        │
                  spec_results[sub.name] = best
```

**四重终止 → LangGraph 条件边映射**（对应现有 `loop.py:67-83`）：

| 条件 | 判断 | 走向 |
|---|---|---|
| ① 达标 | `verdict==PASS` 或 `score >= spec_refine_pass_score` | → finalize |
| ② 迭代上限 | `iteration >= spec_refine_max_iter` | → finalize |
| ③ 分数退化 | `score <= best_score`（震荡/回退） | → finalize |
| ④ 预算超限 | `used_chars > spec_refine_budget_tokens * 4` | → finalize |
| 都不满足 | | → refine → critique（循环） |

**关键细节**：`finalize` 选 `best_spec`（历史最高分版本），不是最后一次 refine 结果（`loop.py:90`）。

## 9. 并发配置 + 架构图

### 9.1 并发

- `spec_refine_concurrency` 默认 **3→4**（`settings.py:82`、`api.ts:254`、`config_store.py:47`）
- **字段名保持不变**（`spec_refine_enabled`/`max_iter`/`pass_score`/`budget_tokens` 整族都不改，单独改一个反而割裂）
- 给 Send fan-out 的 Semaphore 限流用，UI label 已存在（`porto-workbench.tsx:2139`）

### 9.2 Mermaid 更新（`frontend/src/components/architecture-view.tsx`）

`LANGGRAPH` 图（行 65-72）：

```
# 现在                              # 改后
[*] --> retrieve                    [*] --> retrieve
retrieve --> understand             retrieve --> understand
understand --> identify             understand --> identify
identify --> generate               identify --> spec_subgraph: Send fan-out(并发4)
generate --> evaluate               spec_subgraph -->[*]: 各 spec 独立交付
evaluate --> identify  ❌删
evaluate --> [*]      ❌删
```

**另新增** `FILE_SERVICE` 图（store/read_pages/search + pointer 模式），放"架构"tab。

## 10. 错误处理

| 场景 | 处理 |
|---|---|
| 文件损坏/加密/不支持 | `FileService.store` 捕获 → 明确错误返回前端 |
| `read_file` 越界/文件不存在 | tool 返回结构化错误，不崩 session |
| Send 并发撞 LLM rate limit | Semaphore 限流 + 指数退避重试 |
| 子图撞 max_iter | 正常 finalize，用 best_spec |
| LLM 不可用 | 现有降级路径保留（模板 spec / fallback understanding） |

## 11. 测试策略

- **FileService**：store / read_pages / search × {pdf, docx, md, txt}
- **spec 子图**：四重终止各一条用例（达标/上限/退化/预算）
- **Send 限流**：N > 4 时验证分批执行
- **端到端 workflow**：上传 → retrieve → understand → identify → Send → 各 spec 独立产出（无 evaluate）
- **chatbot**：上传 → 文件问答

## 12. 范围与非目标

**本设计范围**：
- FileService（落盘 + 元数据 + 分页读取）
- chatbot 文件上传（前端 + API）
- workflow state pointer 化 + 节点改造
- 删除 evaluate + Send fan-out + spec 子图
- 并发默认值 + Mermaid 更新

**非目标（明确排除）**：
- 图片 vision（render_page 喂多模态）—— 列为后续增强
- pdf-mcp 风格的 SQLite FTS5 全文检索升级 —— 先用简单字符串匹配
- Agent SDK subagent fan-out —— Porto 拆解场景不需要
- 多文件跨文档检索（corpus search）—— 后续

## 13. 关键文件清单

**后端**：
- `backend/src/porto_chatbot/files/service.py`（**新增** FileService）
- `backend/src/porto_chatbot/specs/subgraph.py`（**新增** spec 子图）
- `backend/src/porto_chatbot/agent/agent/state.py`（prd_text → prd_file_id）
- `backend/src/porto_chatbot/agent/agent/graph.py`（删 evaluate + Send）
- `backend/src/porto_chatbot/agent/nodes/{retrieve,understand,generate,evaluate}.py`（改造/删除）
- `backend/src/porto_chatbot/agent_sdk/{tools,backend}.py`（read_file tool 组注册）
- `backend/src/porto_chatbot/tools/handlers.py`（read_file handler）
- `backend/src/porto_chatbot/api/routes/{workflow,chat}.py`（上传改造）
- `backend/src/porto_chatbot/models/chat.py`（ChatRequest.file_ids）
- `backend/src/porto_chatbot/settings.py`（spec_refine_concurrency 默认 4）

**前端**：
- `frontend/src/components/porto-workbench.tsx`（chatbot 附件 UI）
- `frontend/src/components/architecture-view.tsx`（Mermaid 更新 + FILE_SERVICE 图）
- `frontend/src/lib/{api,types}.ts`（上传 API + ChatRequest 类型）
