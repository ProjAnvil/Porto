# 文件服务统一 + Send Fan-out 拆解 Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement task-by-task. Steps use checkbox (`- [ ]`).

> **v2 修正**：经 subagent 审计修正 6 个阻断项。关键决策：**节点名保持 `"generate"`，内部换 Send+子图实现**（A 方案），使 workflow_store SQL / 前端 `outputs.generate` 全不改，只删 evaluate。spec 子图 state 必须声明 `spec_results` 字段才能触发 reducer 合并。

**Goal:** 统一文件服务（memory pointer）+ chatbot 上传 + workflow pointer 化 + 删 evaluate + Send spec 子图。

**Architecture:** `FileService` 作为唯一文件访问层；state 存 `prd_file_id` 指针；spec 生成用 LangGraph `Send` fan-out + evaluator-optimizer 子图（四重终止）；节点名 `"generate"` 不变以兼容存量代码，删除 evaluate。

**Tech Stack:** Python 3.12 / FastAPI / **LangGraph 1.x（实测 1.2.9）** / claude-agent-sdk / pypdf / pydantic / React + assistant-ui / Mermaid

## Global Constraints

- 后端测试：`cd backend && uv run pytest tests/ -x`
- 前端验证：`cd frontend && npm run build`
- 路径不硬编码：用 `settings.data_dir`（默认 `~/.porto`）
- 文件落盘根目录：`settings.data_dir / "files"`
- read_file handler 双注册（`tools/registry.py` + `agent_sdk/tools.py`），零重复
- loguru：`get_component_logger("file_service", settings)`
- commit 每个 task，结尾加 `Co-Authored-By: Claude <noreply@anthropic.com>`
- **节点名决策（A 方案）**：spec 子图注册为节点 `"generate"`（非 `"spec_subgraph"`），保持 workflow_store SQL + 前端 `outputs.generate` 兼容
- **LangGraph 1.2.9 实测**：Send + compiled subgraph 作 `add_node` + reducer 自动合并均支持，**前提是子图 state 声明同名字段**

---

## Phase 1 — FileService 基础设施

### Task 1: 文件元数据 models

**Files:** Create `backend/src/porto_chatbot/models/file.py`；Modify `models/__init__.py`；Test `backend/tests/test_file_service.py`

**Interfaces:** Produces `FileMeta`、`FileInfo`、`FileHit`

- [ ] **Step 1: 写 models** — Create `models/file.py`:
```python
from __future__ import annotations
from datetime import datetime, UTC
from pydantic import BaseModel, Field

class FileMeta(BaseModel):
    file_id: str; owner_id: str; original_name: str; stored_path: str
    mime: str; size_bytes: int; page_count: int
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

class FileInfo(BaseModel):
    file_id: str; original_name: str; mime: str; size_bytes: int; page_count: int

class FileHit(BaseModel):
    page: int; snippet: str
```
在 `models/__init__.py` 追加 `from .file import FileHit, FileInfo, FileMeta`

- [ ] **Step 2: 写测试** — `tests/test_file_service.py` 测三个 model 可构造 + created_at 自动填充

- [ ] **Step 3: 运行** `uv run pytest tests/test_file_service.py -v` → PASS

- [ ] **Step 4: Commit** `feat(file-service): add FileMeta/FileInfo/FileHit models`

---

### Task 2: FileService.store — 落盘 + 分页提取

**Files:** Create `backend/src/porto_chatbot/files/{__init__.py,service.py}`；Modify `settings.py`

**Interfaces:** Consumes `parse_document`（`documents.py:110`，返回 `DocumentArtifact`）；Produces `FileService.store`

- [ ] **Step 1: settings 加路径 property** — `settings.py` 在 `settings_db_path` 附近（:169）加：
```python
@property
def files_db_path(self) -> Path:
    return self.data_dir / "files.sqlite3"

@property
def files_dir(self) -> Path:
    return self.data_dir / "files"
```

- [ ] **Step 2: 写 FileService.store** — Create `files/service.py`。关键点（审计 M6 修正：parse_document 参数补全）：
```python
from __future__ import annotations
import json, sqlite3, uuid
from pathlib import Path
from fastapi import UploadFile
from ..documents import parse_document
from ..models.file import FileMeta
from ..settings import Settings, get_component_logger

_MIME_MAP = {".pdf":"application/pdf", ".docx":"application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".md":"text/markdown", ".txt":"text/plain", ".markdown":"text/markdown"}
_VIRTUAL_PAGE_CHARS = 2000

def _split_virtual_pages(text: str, chars: int = _VIRTUAL_PAGE_CHARS) -> list[str]:
    return [text[i:i+chars] for i in range(0, len(text), chars)] or [""]

class FileService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = get_component_logger("file_service", settings)
        self.settings.files_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.settings.files_db_path)
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self) -> None:
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS files (
                file_id TEXT PRIMARY KEY, owner_id TEXT, original_name TEXT,
                stored_path TEXT, mime TEXT, size_bytes INTEGER, page_count INTEGER,
                pages_json TEXT, created_at TEXT)""")

    def store(self, file: UploadFile, owner_id: str) -> FileMeta:
        file_id = uuid.uuid4().hex[:16]
        original = file.filename or "unnamed"
        suffix = Path(original).suffix.lower()
        mime = _MIME_MAP.get(suffix, "application/octet-stream")
        payload = file.file.read()
        size = len(payload)
        store_dir = self.settings.files_dir / file_id
        store_dir.mkdir(parents=True, exist_ok=True)
        stored_path = store_dir / original
        stored_path.write_bytes(payload)
        # parse_document 参数补全（审计 M6）：max_bytes/document_max_pdf_pages 确认存在于 settings
        max_bytes = getattr(self.settings, "document_max_upload_mb", 20) * 1024 * 1024
        max_pdf = getattr(self.settings, "document_max_pdf_pages", 200)
        artifact = parse_document(stored_path, original_name=original, max_bytes=max_bytes, max_pdf_pages=max_pdf)
        if suffix == ".pdf":
            pages = self._extract_pdf_pages(stored_path)
        else:
            pages = _split_virtual_pages(artifact.text)
        meta = FileMeta(file_id=file_id, owner_id=owner_id, original_name=original,
                        stored_path=str(stored_path), mime=mime, size_bytes=size, page_count=len(pages))
        with self._conn() as c:
            c.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?)",
                      (meta.file_id, owner_id, original, str(stored_path), mime, size,
                       len(pages), json.dumps(pages, ensure_ascii=False), meta.created_at))
        self.logger.info("file store file_id=%s pages=%s size=%s", file_id, len(pages), size)
        return meta

    def _extract_pdf_pages(self, path: Path) -> list[str]:
        from pypdf import PdfReader
        return [(pg.extract_text() or "") for pg in PdfReader(str(path)).pages]

    def _get_row(self, file_id: str) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
```

- [ ] **Step 3: 测试** — store 一个 txt（io.BytesIO 假 UploadFile），断言落盘 + page_count≥1 + 元数据正确

- [ ] **Step 4: 运行 + Commit** `feat(file-service): add FileService.store with pagination`

---

### Task 3: FileService.read_pages / search / get_info

**Files:** Modify `files/service.py`；Test 追加

**Interfaces:** Produces `read_pages`/`search`/`get_info`

- [ ] **Step 1: 追加方法**（get_info/read_pages/search，逻辑见 spec，错误返回结构化字符串不崩）
```python
from ..models.file import FileHit, FileInfo  # 顶部 import 补
class FileService:
    def _pages(self, file_id): ...
    def get_info(self, file_id) -> FileInfo | None: ...      # row→FileInfo
    def read_pages(self, file_id, start, end) -> str:        # 越界/不存在返回错误串
        ...
    def search(self, file_id, query) -> list[FileHit]:       # 小写匹配，snippet ±60 字符
        ...
```

- [ ] **Step 2: 测试** — read_pages 全文 + search 命中 + 越界错误串

- [ ] **Step 3: 运行 + Commit** `feat(file-service): add read_pages/search/get_info`

---

### Task 4: read_file handlers + AgentToolContext + 双注册

**Files:** Modify `tools/context.py`（AgentToolContext）、`tools/handlers.py`、`tools/registry.py`、`agent_sdk/tools.py`；Test `tests/test_file_handlers.py`

**Interfaces:** Produces `_read_file_info`/`_read_file_pages`/`_search_file`；`AgentToolContext.file_service`

- [ ] **Step 1: AgentToolContext 加字段** — `tools/context.py` 的 `@dataclass AgentToolContext`（:36）加 `file_service: Any = None`（有默认值，现有调用点向后兼容）

- [ ] **Step 2: 写 3 个 handler** — `tools/handlers.py` 追加：
```python
def _require_file_service(ctx):
    if ctx.file_service is None: raise RuntimeError("file_service 未注入")
    return ctx.file_service

def _read_file_info(ctx, file_id: str) -> str:
    info = _require_file_service(ctx).get_info(file_id)
    return f"[错误] 文件 {file_id} 不存在" if info is None else f"文件: {info.original_name}\n页数: {info.page_count}\n大小: {info.size_bytes}\n类型: {info.mime}"

def _read_file_pages(ctx, file_id: str, start: int, end: int) -> str:
    return _truncate(_require_file_service(ctx).read_pages(file_id, start, end), _MAX_TOOL_RESULT_CHARS)

def _search_file(ctx, file_id: str, query: str) -> str:
    hits = _require_file_service(ctx).search(file_id, query)
    return f"未找到 '{query}'" if not hits else "\n".join(f"第 {h.page} 页: {h.snippet}" for h in hits)
```

- [ ] **Step 3: 注册 Langchain** — `tools/registry.py` 的 `build_agent_tools` 内，当 `ctx.file_service is not None` 时追加 3 个 `ToolDef`（get_file_info/read_file_pages/search_file，input_schema 见 spec，handler=lambda args: _handler(ctx, ...)）

- [ ] **Step 4: 注册 SDK** — `agent_sdk/tools.py` 的 `build_sdk_tools` 内同模式追加（仿 get_prd_text :76-78 的 `@tool` + `_run_tool`）

- [ ] **Step 5: 测试** — `tests/test_file_handlers.py`：store→ctx.file_service→3 个 handler round-trip

- [ ] **Step 6: 运行 + Commit** `feat(file-service): register read_file tools for both backends`

---

## Phase 2 — workflow pointer 化

### Task 5: state.py — prd_text → prd_file_id

**Files:** Modify `agent/state.py:31-45`

- [ ] **Step 1: 改 state** — `prd_text: str` → `prd_file_id: str`。**保留** `evaluation`/`needs_rework`/`rework_passes` 字段先不删（Task 10 统一清，避免中间态大面积破坏）

- [ ] **Step 2: grep 全部 prd_text 引用** — `cd backend && grep -rn 'prd_text' src/`，已知命中：`nodes/retrieve.py:8`、`nodes/understand.py:78`、`nodes/identify.py:31,67`、`specs/template.py`、`workflow_store.py`、`workflow_executor.py`。在 Task 6/7 改造

- [ ] **Step 3: 编译检查** `uv run python -c "from porto_chatbot.agent.state import PortoAgentState"`

- [ ] **Step 4: Commit** `refactor(state): prd_text -> prd_file_id`

---

### Task 6: workflow upload 路由 + store migration

**Files:** Modify `api/routes/workflow.py:150-218`、`workflow_store.py`、`workflow_executor.py`

> **审计 B3 修正**：workflow_store 无 migration 框架，改列必须加 ALTER TABLE。

- [ ] **Step 1: workflow_store 加 migration** — `workflow_store.py` 的建表后加：
```python
cols = {r[1] for r in conn.execute("PRAGMA table_info(workflows)")}
if "prd_file_id" not in cols:
    conn.execute("ALTER TABLE workflows ADD COLUMN prd_file_id TEXT")
```
`INSERT` 同时写 `prd_text`（空串占位兼容旧读）+ `prd_file_id`。`create` 签名加 `prd_file_id` 参数。

- [ ] **Step 2: workflow_executor fallback** — 读 workflow 行时：`prd_file_id = row["prd_file_id"] or row["prd_text"]`（旧数据 fallback）；initial state 注入 `{"prd_file_id": prd_file_id, ...}`

- [ ] **Step 3: 改 upload 路由** — `workflow.py:150` 把 tempfile 块（:171-191）替换为：
```python
from ..files.service import FileService
meta = FileService(runtime_settings).store(file, owner_id=sid)
store.create(sid, project_name, prd_file_id=meta.file_id, ...)
```

- [ ] **Step 4: 编译 + Commit** `refactor(workflow): upload route uses FileService + prd_file_id (with migration)`

---

### Task 7: 节点改 read_file（含 identify，审计 B5 修正）

**Files:** Modify `nodes/retrieve.py`、`nodes/understand.py`、`nodes/identify.py`、`tools/handlers.py`（_get_prd_text）、`specs/template.py`

> **全局注入点**：所有节点构造 `AgentToolContext(state=state, vector_store=...)` 处补 `file_service=agent.file_service`（agent 实例需挂 file_service，在 deps/agent 构造处注入）。`_get_prd_text` handler 改为：当 ctx.file_service 可用 → `read_pages(state["prd_file_id"], 1, N)`，否则降级旧逻辑。

- [ ] **Step 1: retrieve.py:8** — `state['prd_text'][:2000]` 改为 `file_service.read_pages(state["prd_file_id"], 1, min(5, get_info(...).page_count))[:2000]`

- [ ] **Step 2: understand.py:78 fallback** — `_fallback_understanding` 读 prd_text 改为读前 5 页片段

- [ ] **Step 3: identify.py（审计 B5）** — `:31` `state['prd_text'][:2000]` 和 `:67` `state["prd_text"]` 都改为 `file_service.read_pages(state["prd_file_id"], 1, N)`（N=min(5, page_count)）

- [ ] **Step 4: specs/template.py** — 确认 render_template_spec 不读 prd_text（它读 sources/workflow_id），若读则改

- [ ] **Step 5: _get_prd_text handler** — 改为优先 file_service.read_pages

- [ ] **Step 6: 运行 + Commit** `refactor(nodes): retrieve/understand/identify read via file_service`

---

## Phase 3 — 删 evaluate + Send spec 子图

### Task 8: specs/subgraph.py — evaluator-optimizer 子图

**Files:** Create `specs/subgraph.py`；Test `tests/test_spec_subgraph.py`

> **审计 B1 修正（关键）**：子图 state **必须**声明 `spec_results: Annotated[dict, _dict_merge]`，否则子图产出的 spec_results 会被 LangGraph 静默丢弃，父图永远是空 dict。

**Interfaces:** Consumes `generate_initial_spec`/`critique_spec`/`refine_spec`（steps.py）、`SpecContext`、`_dict_merge`（agent/state.py）；Produces `build_spec_subgraph()`

- [ ] **Step 1: 写子图** — Create `specs/subgraph.py`。子图 state：
```python
from typing import Annotated, Any, TypedDict
from langgraph.graph import END, START, StateGraph
from ..agent.state import _dict_merge          # 复用父图 reducer
from ..models import SpecAttempt, SpecResult
from ..models.enums import SpecVerdict
from .context import SpecContext
from .steps import critique_spec, generate_initial_spec, refine_spec
from .template import render_template_spec

class SpecSubgraphState(TypedDict, total=False):
    sub: Any; prd_file_id: str; current_spec: str; best_spec: str; best_score: int
    used_chars: int; attempts: list; iteration: int; feedback: str
    ctx_backend: Any; ctx_llm: Any; ctx_state: dict; ctx_settings: Any
    ctx_vector_store: Any; ctx_critic_llm: Any
    spec_results: Annotated[dict, _dict_merge]   # ← B1：必须声明，触发父图 reducer 合并
```
节点：`init_spec`（generate_initial_spec，LLM 禁用降级模板）、`critique`（critique_spec + 更新 attempts/best_score/feedback/iteration）、`refine`（refine_spec）、`finalize`（选 best_spec，return `{"spec_results": {sub.name: SpecResult(...)}}`）

条件边 `_should_stop` 四重终止：① `verdict==PASS or score>=pass_score` ② `iteration>=max_iter` ③ `score < best_score`（退化）④ `used_chars > budget*4` → 任一满足走 finalize，否则 refine→critique 循环

图：`START→init_spec→critique→[should_stop]→{finalize | refine→critique}→END`

- [ ] **Step 2: 单元测试** — `_should_stop` 四分支 + init 降级路径

- [ ] **Step 3: 集成测试（审计 M8）** — 真跑子图（mock LLM）2 个 subsystem，断言 `spec_results` 有 2 个 key + SpecResult 字段完整

- [ ] **Step 4: 运行 + Commit** `feat(specs): evaluator-optimizer subgraph with spec_results reducer`

---

### Task 9: graph.py — Send fan-out（节点名保持 generate，审计 B2 A 方案）

**Files:** Modify `agent/graph.py`、`agent/nodes/generate.py`

> **审计 B2/M2 修正**：节点名注册为 `"generate"`（非 spec_subgraph），workflow_store SQL（`WHERE step_name='generate'`）+ 前端 `outputs.generate` 全不改。STEPS 删 evaluate 保留 generate。

- [ ] **Step 1: generate.py 改 Send 派发** — 删 ThreadPoolExecutor 块（:29-40），改为：
```python
from langgraph.types import Send

def dispatch_specs(state, *, config):
    agent = config["configurable"]["agent"]
    base = {"ctx_backend": agent.backend, "ctx_llm": agent.llm, "ctx_state": {**state},
            "ctx_settings": agent.settings, "ctx_vector_store": agent.vector_store,
            "ctx_critic_llm": agent.critic_llm, "prd_file_id": state.get("prd_file_id")}
    return [Send("generate", {"sub": sub, **base}) for sub in state["subsystems"]]
```

- [ ] **Step 2: graph.py 改拓扑** — `build_workflow_graph`：
```python
from ..specs.subgraph import build_spec_subgraph

STEPS = ["retrieve", "understand", "identify", "generate"]   # 删 evaluate
INTERRUPT_AFTER = ["understand", "identify", "generate"]     # 保留 generate → 用户审计 spec

def build_workflow_graph(checkpointer):
    g = StateGraph(PortoAgentState)
    g.add_node("retrieve", retrieve_node.retrieve_knowledge)
    g.add_node("understand", understand_node.understand_prd)
    g.add_node("identify", identify_node.identify_subsystems)
    g.add_node("generate", build_spec_subgraph())   # 节点名 generate，内部是子图
    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "understand")
    g.add_edge("understand", "identify")
    g.add_conditional_edges("identify", generate_node.dispatch_specs, ["generate"])
    g.add_edge("generate", END)
    return g.compile(checkpointer=checkpointer, interrupt_after=INTERRUPT_AFTER)
```
删 evaluate 节点注册、generate→evaluate 边、evaluate→identify 回边。

- [ ] **Step 3: Semaphore 限流（审计 M3）** — 挂 agent 实例而非模块级。agent 构造时 `self._spec_sema = threading.Semaphore(settings.spec_refine_concurrency)`；子图 init_spec 入口 `with agent._spec_sema:`（agent 从 ctx_settings 旁路传入，或在 SpecSubgraphState 加 `ctx_sema` 字段由 dispatch_specs 注入）。用 `with` 保证异常释放。

- [ ] **Step 4: 编译** `uv run python -c "from porto_chatbot.agent.graph import build_workflow_graph; print('ok')"`

- [ ] **Step 5: Commit** `feat(graph): Send fan-out spec subgraph (node name generate), remove evaluate`

---

### Task 10: 清理 evaluate + rework（rework 字段保留为废弃，审计 M4）

**Files:** Delete `agent/nodes/evaluate.py`；Modify `agent/nodes/__init__.py`、`graph.py`（清 import）

- [ ] **Step 1: 搜残留** `grep -rn 'needs_rework\|rework_passes\|evaluate_node' src/`。删 evaluate.py + __init__ 导出 + graph.py import。

- [ ] **Step 2: rework 字段保留** — `workflow_rework_enabled`/`workflow_rework_max_passes`（settings.py:87-88、config_store 白名单、前端 AgentConfig）**保留为废弃字段**（最小改动，不再被 graph 使用）。state.py 的 `needs_rework`/`rework_passes`/`evaluation` 在确认无引用后删除。

- [ ] **Step 3: 运行 + Commit** `refactor: remove evaluate node (rework fields kept as deprecated)`

---

## Phase 4 — chatbot 上传

### Task 11: 后端 /api/chat/files + ChatRequest.file_ids

**Files:** Modify `models/chat.py`、`api/routes/chat.py`、`api/sse.py`

- [ ] **Step 1: ChatRequest 加 file_ids** — `models/chat.py:18` 加 `file_ids: list[str] = Field(default_factory=list)`

- [ ] **Step 2: FileService deps 单例（审计 M5）** — `api/deps.py` 加 `get_file_service(settings) -> FileService`（仿 get_workflow_store 模式）

- [ ] **Step 3: 上传端点（审计 M7：同步 def 走线程池）** — `api/routes/chat.py` 追加：
```python
@router.post("/api/chat/files")
def upload_chat_file(file: Annotated[UploadFile, File()], session_id: Annotated[str, Form()] = "default"):
    meta = get_file_service(get_settings()).store(file, owner_id=session_id)
    return {"file_id": meta.file_id, "page_count": meta.page_count, "original_name": meta.original_name}
```

- [ ] **Step 4: stream body 解析 file_ids** — `api/sse.py` 的 `_chat_request_from_stream_body` 把 body["file_ids"] 传进 ChatRequest

- [ ] **Step 5: 编译 + Commit** `feat(chat): add /api/chat/files + ChatRequest.file_ids`

---

### Task 12: agent_sdk chat 注入 file_service + read_file MCP tool

**Files:** Modify `agent_sdk/backend.py`（_build_chat_options :315）

- [ ] **Step 1: 注入 file_service** — `_build_chat_options` 构造 AgentToolContext（:341）补 `file_service=get_file_service(settings)`；read_file MCP tool 已在 Task 4 注册，chat 路径自动可用

- [ ] **Step 2: system prompt 提示** — 若 `req.file_ids` 非空，system prompt 追加：`本次对话关联文件 {file_ids}，可调用 get_file_info/read_file_pages/search_file 读取`

- [ ] **Step 3: 编译 + Commit** `feat(chat): inject file_service + file_ids into agent sdk chat`

---

### Task 13: 前端聊天附件 UI

**Files:** Modify `porto-workbench.tsx`（Composer :1071、ChatSession transport :800）、`lib/api.ts`

> 行号仅供参考，按组件名/字符串特征定位（审计 S4）

- [ ] **Step 1: api.ts 加 uploadChatFile** — 仿 createWorkflowUpload（:99）：
```ts
export async function uploadChatFile(file: File, sessionId: string) {
  const form = new FormData();
  form.set("file", file); form.set("session_id", sessionId);
  return parseJson(await fetch("/api/chat/files", { method: "POST", body: form }));
}
```

- [ ] **Step 2: Composer 加附件按钮** — 隐藏 `<input type="file" multiple accept=".pdf,.docx,.md,.txt">` + Upload 图标，已选文件名显示在输入框上方；附件 state 提升到 ChatSession

- [ ] **Step 3: transport body 带 file_ids** — ChatSession 的 AssistantChatTransport body 追加 `file_ids`，发送后清空

- [ ] **Step 4: build + Commit** `feat(chat-ui): attachment upload in chat composer`

---

### Task 14（审计 B4）: 前端 step 类型同步（删 evaluate）

**Files:** Modify `lib/types.ts`、`porto-workbench.tsx`

> A 方案：节点名 generate 保持，只删 evaluate 相关。generate 保留 → outputs.generate / CHECKPOINT_STEPS / 用户审计 spec 全不受影响。

- [ ] **Step 1: types.ts:183** — `WorkflowStepName` 删 `"evaluate"`（保留 generate）

- [ ] **Step 2: porto-workbench.tsx** — `WORKFLOW_STEPS`(:96)、`CHECKPOINT_STEPS`(:104)、`STEP_LABELS`(:110)、`RERUN_STEPS`(:2515) 删 evaluate 项（generate 保留）

- [ ] **Step 3: build + Commit** `refactor(frontend): drop evaluate from WorkflowStepName (generate kept)`

---

## Phase 5 — 并发配置 + Mermaid

### Task 15: spec_refine_concurrency 默认 4

**Files:** Modify `settings.py:82`、`lib/api.ts:254`

- [ ] **Step 1:** `settings.py` `spec_refine_concurrency: int = Field(default=4, ge=1, le=10)`
- [ ] **Step 2:** `api.ts` `spec_refine_concurrency: 4,`
- [ ] **Step 3: Commit** `chore: bump spec_refine_concurrency default to 4`

---

### Task 16: architecture-view.tsx 更新

**Files:** Modify `architecture-view.tsx`

> 审计 M9：不仅改 LANGGRAPH 常量，"系统定位"段（:121-123）也要改。

- [ ] **Step 1: LANGGRAPH 图（:65-72）** 替换为：
```
stateDiagram-v2
    [*] --> retrieve: prd_file_id (pointer)
    retrieve --> understand: + sources
    understand --> identify: + understanding
    identify --> generate: Send fan-out (并发=spec_refine_concurrency)
    generate --> [*]: 各 spec 独立交付（无 evaluate）
```

- [ ] **Step 2: "系统定位"段（:121-123）** 把 `retrieve → understand → identify → generate → evaluate` 改为去掉 evaluate

- [ ] **Step 3: DiagramSection description（:155-159）** 去掉 evaluate/needs_rework 描述

- [ ] **Step 4: 新增 FILE_SERVICE 图** — 加常量 + DiagramSection（store→落盘+pages_json→read_pages/search→workflow/chatbot 按需读）

- [ ] **Step 5: build + Commit** `docs(arch): update diagrams (drop evaluate, add FILE_SERVICE)`

---

## 收尾

### Task 17: 端到端冒烟

- [ ] **Step 1:** `cd backend && uv run pytest tests/ -x`
- [ ] **Step 2:** `cd frontend && npm run build`
- [ ] **Step 3:** 手动：上传 PRD 跑 workflow（验证 retrieve→understand→identify→generate(Send 子图)→各 spec 独立产出，无 evaluate，generate 中断可审计）；chatbot 上传文件提问（read_file tool 被调用）
- [ ] **Step 4:** `git commit -am "test: end-to-end smoke validation"`
