# 文件服务统一 + Send Fan-out 拆解 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 统一文件服务（memory pointer 模式）+ chatbot 文件上传 + workflow state pointer 化 + 删除 evaluate 节点 + L3 Send fan-out spec 子图。

**Architecture:** 新增 `FileService` 作为唯一文件访问层（落盘 + 分页读取），workflow 与 chatbot 共用。state 只存 `prd_file_id` 指针不存全文。spec 生成从 `ThreadPoolExecutor` 换成 LangGraph `Send` fan-out + evaluator-optimizer 子图（四重终止）。删除 evaluate 全局关卡。

**Tech Stack:** Python 3.12 / FastAPI / LangGraph 0.4+ / claude-agent-sdk / pypdf / pydantic / React + assistant-ui + Next.js / Mermaid

## Global Constraints

- **Python ≥ 3.12**，依赖管理用 `uv`，后端在 `backend/` 目录
- **后端测试**：`cd backend && uv run pytest tests/ -x` （pytest + conftest.py）
- **前端验证**：`cd frontend && npm run build`（含 tsc 类型检查）；前端无单测框架，靠 build + 手动验证
- **data_dir 不硬编码**：所有路径用 `settings.data_dir`（默认 `~/.porto`）
- **文件落盘根目录**：`settings.data_dir / "files"`
- **CLAUDE.md 规则**：删除操作需用户确认（本 plan 的"删除"指删代码节点，非删用户数据；执行时由授权的 subagent 自主完成，无需逐次问用户——用户已全权授权）
- **DRY**：read_file handler 同时服务 LangchainBackend（`tools/registry.py`）和 AgentSDKBackend（`agent_sdk/tools.py`），零重复
- **loguru 日志**：新组件用 `get_component_logger("file_service", settings)`，输出到 app.log + stderr
- **commit 粒度**：每个 task 结束 commit 一次，message 用 `feat:`/`refactor:`/`test:` 前缀，结尾加 `Co-Authored-By: Claude <noreply@anthropic.com>`

## 接口契约索引（全局，供跨 task 参考）

- `FileService`（Phase 1 产出）：`store(file, owner_id) -> FileMeta`、`get_info(file_id) -> FileInfo`、`read_pages(file_id, start, end) -> str`、`search(file_id, query) -> list[FileHit]`
- `FileMeta` / `FileInfo` / `FileHit`（Task 1 产出，`models/file.py`）
- read_file handlers（Task 4 产出，`tools/handlers.py`）：`_read_file_info(ctx, file_id)`、`_read_file_pages(ctx, file_id, start, end)`、`_search_file(ctx, file_id, query)`
- `PortoAgentState.prd_file_id: str`（Task 5 产出，替代 `prd_text`）
- `build_spec_subgraph()`（Task 8 产出，`specs/subgraph.py`）
- `AgentToolContext` 新增 `file_service: FileService | None = None`（Task 4）

---

## Phase 1 — FileService 基础设施

### Task 1: 文件元数据 models + files 表

**Files:**
- Create: `backend/src/porto_chatbot/models/file.py`
- Modify: `backend/src/porto_chatbot/models/__init__.py`（导出新模型）
- Test: `backend/tests/test_file_service.py`

**Interfaces:**
- Produces: `FileMeta`、`FileInfo`、`FileHit`（pydantic models，定义如下）

- [ ] **Step 1: 写 models**

Create `backend/src/porto_chatbot/models/file.py`:
```python
from __future__ import annotations

from datetime import datetime, UTC
from pydantic import BaseModel, Field


class FileMeta(BaseModel):
    """文件落盘后的元数据（store 返回，存 sqlite）。"""
    file_id: str
    owner_id: str
    original_name: str
    stored_path: str
    mime: str
    size_bytes: int
    page_count: int
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class FileInfo(BaseModel):
    """get_file_info tool 返回的摘要。"""
    file_id: str
    original_name: str
    mime: str
    size_bytes: int
    page_count: int


class FileHit(BaseModel):
    """search_file 的单条命中。"""
    page: int
    snippet: str
```

In `backend/src/porto_chatbot/models/__init__.py`，追加导出：
```python
from .file import FileHit, FileInfo, FileMeta
```

- [ ] **Step 2: 写测试 — models 可构造**

In `backend/tests/test_file_service.py`:
```python
from porto_chatbot.models import FileHit, FileInfo, FileMeta


def test_file_meta_defaults():
    m = FileMeta(file_id="f1", owner_id="s1", original_name="a.pdf",
                 stored_path="/tmp/a.pdf", mime="application/pdf",
                 size_bytes=100, page_count=3)
    assert m.created_at  # 自动填充
    assert m.page_count == 3


def test_file_hit_and_info():
    assert FileInfo(file_id="f1", original_name="a.pdf", mime="application/pdf",
                    size_bytes=100, page_count=3).page_count == 3
    assert FileHit(page=2, snippet="...").page == 2
```

- [ ] **Step 3: 运行测试验证通过**

Run: `cd backend && uv run pytest tests/test_file_service.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add backend/src/porto_chatbot/models/file.py backend/src/porto_chatbot/models/__init__.py backend/tests/test_file_service.py
git commit -m "feat(file-service): add FileMeta/FileInfo/FileHit models"
```

---

### Task 2: FileService.store — 落盘 + 分页提取 + 元数据

**Files:**
- Create: `backend/src/porto_chatbot/files/__init__.py`
- Create: `backend/src/porto_chatbot/files/service.py`
- Modify: `backend/src/porto_chatbot/settings.py`（加 `files_db_path` property）
- Test: `backend/tests/test_file_service.py`（追加）

**Interfaces:**
- Consumes: `settings.data_dir`、`parse_document`（`documents.py:110`，返回 `DocumentArtifact` 含 `page_count`）
- Produces: `FileService` 类，`store(file: UploadFile, owner_id: str) -> FileMeta`

- [ ] **Step 1: settings 加 files_db_path property**

In `backend/src/porto_chatbot/settings.py`，在 `settings_db_path` property 附近（约 :169）追加：
```python
@property
def files_db_path(self) -> Path:
    return self.data_dir / "files.sqlite3"

@property
def files_dir(self) -> Path:
    return self.data_dir / "files"
```

- [ ] **Step 2: 写 FileService.store + 建表 + 分页提取**

Create `backend/src/porto_chatbot/files/__init__.py`（空文件，标记 package）。

Create `backend/src/porto_chatbot/files/service.py`:
```python
"""统一文件服务：落盘 + 分页提取 + 按需读取。Memory pointer 模式的后端。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from fastapi import UploadFile

from ..documents import parse_document
from ..models.file import FileMeta
from ..settings import Settings, get_component_logger

_MIME_MAP = {
    ".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown", ".txt": "text/plain", ".markdown": "text/markdown",
}
_VIRTUAL_PAGE_CHARS = 2000  # 非_pdf 文件的虚拟分页大小


def _split_virtual_pages(text: str, chars: int = _VIRTUAL_PAGE_CHARS) -> list[str]:
    return [text[i:i + chars] for i in range(0, len(text), chars)] or [""]


class FileService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = get_component_logger("file_service", settings)
        self.settings.files_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.settings.files_db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS files (
                    file_id TEXT PRIMARY KEY, owner_id TEXT, original_name TEXT,
                    stored_path TEXT, mime TEXT, size_bytes INTEGER, page_count INTEGER,
                    pages_json TEXT, created_at TEXT
                )"""
            )

    def store(self, file: UploadFile, owner_id: str) -> FileMeta:
        file_id = uuid.uuid4().hex[:16]
        original = file.filename or "unnamed"
        suffix = Path(original).suffix.lower()
        mime = _MIME_MAP.get(suffix, "application/octet-stream")
        payload = file.file.read()
        size = len(payload)

        # 落盘
        store_dir = self.settings.files_dir / file_id
        store_dir.mkdir(parents=True, exist_ok=True)
        stored_path = store_dir / original
        stored_path.write_bytes(payload)

        # 解析 + 分页提取
        artifact = parse_document(stored_path, original_name=original,
                                  max_pdf_pages=self.settings.document_max_pdf_pages)
        if suffix == ".pdf":
            pages = self._extract_pdf_pages(stored_path)
        else:
            pages = _split_virtual_pages(artifact.text)
        page_count = len(pages)

        meta = FileMeta(file_id=file_id, owner_id=owner_id, original_name=original,
                        stored_path=str(stored_path), mime=mime, size_bytes=size,
                        page_count=page_count)
        with self._conn() as c:
            c.execute(
                "INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?)",
                (meta.file_id, owner_id, original, str(stored_path), mime, size,
                 page_count, json.dumps(pages, ensure_ascii=False), meta.created_at),
            )
        self.logger.info("file store file_id=%s owner=%s pages=%s size=%s",
                         file_id, owner_id, page_count, size)
        return meta

    def _extract_pdf_pages(self, path: Path) -> list[str]:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        return [(pg.extract_text() or "") for pg in reader.pages]

    def _get_row(self, file_id: str) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
```

> 注：`parse_document` 的 `max_pdf_pages` 参数从 `runtime_settings.document_max_pdf_pages` 来；这里简化用 `self.settings.document_max_pdf_pages`（Settings 上若无此字段，用 `getattr(self.settings, "document_max_pdf_pages", 200)`）。执行时确认 Settings 字段名。

- [ ] **Step 3: 写测试 — store 落盘 + 元数据**

In `backend/tests/test_file_service.py` 追加（用 io.BytesIO 构造假 UploadFile）:
```python
import io
from fastapi import UploadFile
from porto_chatbot.files.service import FileService, _split_virtual_pages


def _fake_upload(name: str, content: bytes) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(content))


def test_split_virtual_pages():
    assert _split_virtual_pages("abcdefgh", chars=3) == ["abc", "def", "gh"]
    assert _split_virtual_pages("", chars=3) == [""]


def test_store_txt(tmp_path, monkeypatch):
    from porto_chatbot.settings import Settings
    s = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs")
    svc = FileService(s)
    meta = svc.store(_fake_upload("note.txt", b"hello world " * 100), owner_id="sess1")
    assert meta.page_count >= 1
    assert meta.owner_id == "sess1"
    assert (tmp_path / "files" / meta.file_id / "note.txt").exists()
```

- [ ] **Step 4: 运行测试**

Run: `cd backend && uv run pytest tests/test_file_service.py -v`
Expected: PASS（若 Settings 字段名不符，按实际修正）

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/files/ backend/src/porto_chatbot/settings.py backend/tests/test_file_service.py
git commit -m "feat(file-service): add FileService.store with pagination extraction"
```

---

### Task 3: FileService.read_pages / search / get_info

**Files:**
- Modify: `backend/src/porto_chatbot/files/service.py`
- Test: `backend/tests/test_file_service.py`（追加）

**Interfaces:**
- Produces: `read_pages(file_id, start, end) -> str`、`search(file_id, query) -> list[FileHit]`、`get_info(file_id) -> FileInfo | None`

- [ ] **Step 1: 实现 read_pages / search / get_info**

In `backend/src/porto_chatbot/files/service.py`，给 `FileService` 追加方法：
```python
from ..models.file import FileHit, FileInfo  # 追加到顶部 import

class FileService:
    # ... 已有 store / _init_db ...

    def _pages(self, file_id: str) -> list[str] | None:
        row = self._get_row(file_id)
        if row is None:
            return None
        return json.loads(row["pages_json"])

    def get_info(self, file_id: str) -> FileInfo | None:
        row = self._get_row(file_id)
        if row is None:
            return None
        return FileInfo(file_id=row["file_id"], original_name=row["original_name"],
                        mime=row["mime"], size_bytes=row["size_bytes"],
                        page_count=row["page_count"])

    def read_pages(self, file_id: str, start: int, end: int) -> str:
        pages = self._pages(file_id)
        if pages is None:
            return f"[错误] 文件 {file_id} 不存在"
        start = max(1, start)
        end = min(end, len(pages))
        if start > end:
            return f"[错误] 页码范围无效，文件共 {len(pages)} 页"
        return "\n\n".join(f"--- 第 {i} 页 ---\n{pages[i-1]}" for i in range(start, end + 1))

    def search(self, file_id: str, query: str) -> list[FileHit]:
        pages = self._pages(file_id)
        if pages is None:
            return []
        q = query.lower()
        hits: list[FileHit] = []
        for idx, text in enumerate(pages, start=1):
            pos = text.lower().find(q)
            if pos >= 0:
                snippet_start = max(0, pos - 60)
                hits.append(FileHit(page=idx, snippet=text[snippet_start:pos + len(q) + 60]))
        return hits
```

- [ ] **Step 2: 写测试 — read_pages / search / get_info**

追加到 `backend/tests/test_file_service.py`:
```python
def test_read_pages_and_search(tmp_path):
    from porto_chatbot.settings import Settings
    s = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs")
    svc = FileService(s)
    meta = svc.store(_fake_upload("note.txt", b"alpha beta gamma delta epsilon"), owner_id="s1")
    info = svc.get_info(meta.file_id)
    assert info is not None and info.original_name == "note.txt"
    full = svc.read_pages(meta.file_id, 1, info.page_count)
    assert "alpha" in full
    hits = svc.search(meta.file_id, "gamma")
    assert hits and hits[0].page >= 1 and "gamma" in hits[0].snippet
    # 越界
    assert "不存在" in svc.read_pages("nope", 1, 1)
```

- [ ] **Step 3: 运行测试**

Run: `cd backend && uv run pytest tests/test_file_service.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add backend/src/porto_chatbot/files/service.py backend/tests/test_file_service.py
git commit -m "feat(file-service): add read_pages/search/get_info"
```

---

### Task 4: read_file handlers + AgentToolContext + 双注册

**Files:**
- Modify: `backend/src/porto_chatbot/tools/context.py`（AgentToolContext 加 `file_service`）
- Modify: `backend/src/porto_chatbot/tools/handlers.py`（加 3 个 handler）
- Modify: `backend/src/porto_chatbot/tools/registry.py`（Langchain 注册）
- Modify: `backend/src/porto_chatbot/agent_sdk/tools.py`（SDK 注册）
- Test: `backend/tests/test_file_handlers.py`

**Interfaces:**
- Consumes: `FileService`（Task 2/3）
- Produces: `_read_file_info(ctx, file_id)`、`_read_file_pages(ctx, file_id, start, end)`、`_search_file(ctx, file_id, query)`

- [ ] **Step 1: AgentToolContext 加 file_service 字段**

In `backend/src/porto_chatbot/tools/context.py`（`AgentToolContext` dataclass，约 :36），追加字段：
```python
file_service: Any = None  # FileService（chatbot + workflow 都注入）或 None
```
（`Any` 已在文件顶部 import；保持向后兼容，默认 None）

- [ ] **Step 2: 写 3 个 handler**

In `backend/src/porto_chatbot/tools/handlers.py`，追加：
```python
def _require_file_service(ctx: AgentToolContext):
    if ctx.file_service is None:
        raise RuntimeError("file_service 未注入 AgentToolContext")
    return ctx.file_service


def _read_file_info(ctx: AgentToolContext, file_id: str) -> str:
    info = _require_file_service(ctx).get_info(file_id)
    if info is None:
        return f"[错误] 文件 {file_id} 不存在"
    return (f"文件: {info.original_name}\n页数: {info.page_count}\n"
            f"大小: {info.size_bytes} bytes\n类型: {info.mime}")


def _read_file_pages(ctx: AgentToolContext, file_id: str, start: int, end: int) -> str:
    return _truncate(_require_file_service(ctx).read_pages(file_id, start, end), _MAX_TOOL_RESULT_CHARS)


def _search_file(ctx: AgentToolContext, file_id: str, query: str) -> str:
    hits = _require_file_service(ctx).search(file_id, query)
    if not hits:
        return f"未在文件 {file_id} 中找到 '{query}'"
    return "\n".join(f"第 {h.page} 页: {h.snippet}" for h in hits)
```

- [ ] **Step 3: 注册到 Langchain registry**

In `backend/src/porto_chatbot/tools/registry.py`（`build_agent_tools` 内，现有 6 个 tool 旁边），追加 3 个 `ToolDef`，模式与现有 `get_prd_text` 一致：
```python
ToolDef(name="get_file_info", description="获取已上传文件的元信息（页数/大小/类型）",
        input_schema={"type": "object", "properties": {"file_id": {"type": "string"}},
                      "required": ["file_id"]},
        handler=lambda args: _read_file_info(ctx, args["file_id"])),
ToolDef(name="read_file_pages", description="读取文件指定页码范围的文本（start/end 为页码）",
        input_schema={"type": "object",
                      "properties": {"file_id": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}},
                      "required": ["file_id", "start", "end"]},
        handler=lambda args: _read_file_pages(ctx, args["file_id"], args["start"], args["end"])),
ToolDef(name="search_file", description="在文件内搜索关键词，返回命中页码与片段",
        input_schema={"type": "object",
                      "properties": {"file_id": {"type": "string"}, "query": {"type": "string"}},
                      "required": ["file_id", "query"]},
        handler=lambda args: _search_file(ctx, args["file_id"], args["query"])),
```
（import `_read_file_info, _read_file_pages, _search_file` from `.handlers`；仅当 `ctx.file_service is not None` 时注册这 3 个）

- [ ] **Step 4: 注册到 SDK tools（同模式）**

In `backend/src/porto_chatbot/agent_sdk/tools.py`（`build_sdk_tools` 内），照现有 `get_prd_text`（:76-78）的模式，追加 3 个 tool 定义（用 `@tool` 装饰器 + `_run_tool` 分发）。仅当 `ctx.file_service is not None` 时追加。

- [ ] **Step 5: 写测试 — handler 走通 FileService**

Create `backend/tests/test_file_handlers.py`:
```python
import io
from fastapi import UploadFile
from porto_chatbot.files.service import FileService
from porto_chatbot.tools.context import AgentToolContext
from porto_chatbot.tools.handlers import _read_file_info, _read_file_pages, _search_file


def test_handlers_round_trip(tmp_path):
    from porto_chatbot.settings import Settings
    svc = FileService(Settings(data_dir=tmp_path, log_dir=tmp_path / "logs"))
    meta = svc.store(UploadFile(filename="n.txt", file=io.BytesIO(b"hello world world")), owner_id="s1")
    ctx = AgentToolContext(state={}, file_service=svc)
    assert "n.txt" in _read_file_info(ctx, meta.file_id)
    assert "hello" in _read_file_pages(ctx, meta.file_id, 1, 1)
    assert "world" in _search_file(ctx, meta.file_id, "world")
```

- [ ] **Step 6: 运行测试 + Commit**

Run: `cd backend && uv run pytest tests/test_file_handlers.py tests/test_file_service.py -v`
```bash
git add backend/src/porto_chatbot/tools/ backend/src/porto_chatbot/agent_sdk/tools.py backend/tests/test_file_handlers.py
git commit -m "feat(file-service): register read_file tools for both backends"
```

---

## Phase 2 — workflow pointer 化

### Task 5: state.py — prd_text → prd_file_id

**Files:**
- Modify: `backend/src/porto_chatbot/agent/state.py:31-45`
- Modify: 所有引用 `state["prd_text"]` / `state.get("prd_text")` 的节点

**Interfaces:**
- Produces: `PortoAgentState.prd_file_id: str`（替代 `prd_text`）

- [ ] **Step 1: 改 state 定义**

In `backend/src/porto_chatbot/agent/state.py`，把 `prd_text: str` 改为：
```python
prd_file_id: str
```
删除 `rework_passes: int` 和 `needs_rework: bool`（Phase 3 删 evaluate 配套）。保留 `evaluation` 字段先不删（避免大面积破坏，evaluate 节点删除时一并清）。

- [ ] **Step 2: 全局搜索 prd_text 引用并标记**

Run: `cd backend && grep -rn 'prd_text' src/porto_chatbot/`，列出所有命中文件。已知命中：`nodes/retrieve.py:8`、`nodes/understand.py:78`、`nodes/identify.py`（待确认）、`specs/template.py`（render_template_spec 读 state）、`api/routes/workflow.py`（store.create 写 prd_text）。

- [ ] **Step 3: 逐个改造引用点**

每处 `state["prd_text"]` / `state.get("prd_text")` 改为读 `state["prd_file_id"]` 并通过 `ctx.file_service.read_pages(...)` 按需取文本。具体改法在 Task 6（upload）、Task 7（节点）里完成。本 task 只改 state 定义 + 确认无遗漏引用（编译检查）。

- [ ] **Step 4: 编译检查 + Commit**

Run: `cd backend && uv run python -c "from porto_chatbot.agent.state import PortoAgentState; print('ok')"`
```bash
git add backend/src/porto_chatbot/agent/state.py
git commit -m "refactor(state): prd_text -> prd_file_id (pointer mode)"
```

---

### Task 6: workflow upload 路由改走 FileService

**Files:**
- Modify: `backend/src/porto_chatbot/api/routes/workflow.py:150-218`
- Modify: `backend/src/porto_chatbot/workflow_store.py`（store.create 签名，若它存 prd_text）

**Interfaces:**
- Consumes: `FileService.store`
- Produces: upload 后 state 拿到 `prd_file_id`（而非 `prd_text`）

- [ ] **Step 1: 改 upload 路由**

In `backend/src/porto_chatbot/api/routes/workflow.py`，把 `upload_workflow`（:150-218）的 tempfile 块（:171-191）替换为 FileService.store：
```python
from ..files.service import FileService  # 顶部 import

# 替换 tempfile + parse_document + unlink 块：
file_service = FileService(runtime_settings)  # 或从 deps 注入单例
meta = file_service.store(file, owner_id=sid)
```
然后 `store.create(...)` 调用里，把原来传 `text.strip()` 的位置改为传 `prd_file_id=meta.file_id`（需同步改 `workflow_store.create` 签名存 `prd_file_id` 而非 `prd_text`）。

> 注：`workflow_store.create` 当前签名存 `text`。改为存 `prd_file_id`。读侧（workflow_executor 启动 graph 时注入 initial state）把 `prd_file_id` 放进 state。

- [ ] **Step 2: 改 workflow_store.create 签名**

In `backend/src/porto_chatbot/workflow_store.py`，`create` 方法的 `text` 参数改为 `prd_file_id`，存进 sqlite 的列也对应改名（migration：加 `prd_file_id` 列，保留旧 `prd_text` 列兼容已存数据或写空）。

- [ ] **Step 3: 改 workflow_executor 注入 initial state**

找到 workflow_executor 启动 graph 的地方（`api/deps.py` 附近或 `workflow_executor.py`），确保 initial state 用 `{"prd_file_id": ..., "project_name": ...}` 而非 `{"prd_text": ...}`。

- [ ] **Step 4: 手动验证 + Commit**

Run: `cd backend && uv run pytest tests/ -x -k workflow`（若有 workflow 测试）；否则 `uv run python -c "import porto_chatbot.api.routes.workflow"`
```bash
git add backend/src/porto_chatbot/api/routes/workflow.py backend/src/porto_chatbot/workflow_store.py
git commit -m "refactor(workflow): upload route uses FileService + prd_file_id"
```

---

### Task 7: retrieve / understand / identify 节点改 read_file

**Files:**
- Modify: `backend/src/porto_chatbot/agent/nodes/retrieve.py`
- Modify: `backend/src/porto_chatbot/agent/nodes/understand.py`
- Modify: `backend/src/porto_chatbot/agent/nodes/identify.py`
- Modify: `backend/src/porto_chatbot/tools/handlers.py`（`_get_prd_text` 改为读 file_service）
- Modify: `backend/src/porto_chatbot/specs/template.py`（render_template_spec 不再读全文）

**Interfaces:**
- Consumes: `ctx.file_service`（注入到节点的 AgentToolContext）、`state["prd_file_id"]`

- [ ] **Step 1: 改 _get_prd_text handler**

`_get_prd_text`（handlers.py:16）当前返回 `state["prd_text"]` 前若干字符。改为：当 `ctx.file_service` 可用时 `read_pages(state["prd_file_id"], 1, N)`，否则降级旧逻辑。同时给节点的 `AgentToolContext(state=state, vector_store=...)` 构造处补 `file_service=...`（从 agent/settings 注入）。

- [ ] **Step 2: 改 retrieve 节点**

`retrieve.py:8` 把 `state['prd_text'][:2000]` 改为从 file_service 读前 N 页拼接：
```python
file_service = ...  # 从 config/agent 注入
head = file_service.read_pages(state["prd_file_id"], 1, min(5, file_service.get_info(state["prd_file_id"]).page_count))
query = f"{state['project_name']}\n{head[:2000]}"
```

- [ ] **Step 3: 改 understand fallback**

`understand.py:78` `_fallback_understanding` 当前直接读 `state["prd_text"]`。改为读前 N 页片段做启发式。

- [ ] **Step 4: 改 specs/template.py**

`render_template_spec` 读 `state.get('sources')` 等，不读 prd_text 全文——确认它不依赖 prd_text（若依赖则改 file_service 读片段）。

- [ ] **Step 5: 编译 + Commit**

Run: `cd backend && uv run pytest tests/ -x`
```bash
git add backend/src/porto_chatbot/agent/nodes/ backend/src/porto_chatbot/tools/handlers.py backend/src/porto_chatbot/specs/template.py
git commit -m "refactor(nodes): retrieve/understand/identify read via file_service"
```

---

## Phase 3 — 删 evaluate + Send spec 子图

### Task 8: specs/subgraph.py — evaluator-optimizer 子图

**Files:**
- Create: `backend/src/porto_chatbot/specs/subgraph.py`
- Test: `backend/tests/test_spec_subgraph.py`

**Interfaces:**
- Consumes: `generate_initial_spec`、`critique_spec`、`refine_spec`（`specs/steps.py`）、`SpecContext`、`Subsystem`、`SpecAttempt`、`SpecVerdict`
- Produces: `build_spec_subgraph() -> CompiledGraph`，子图 state 见下，输出 `{"spec_results": {sub.name: SpecResult}}`

**子图 state（新增 TypedDict）:**
```python
class SpecSubgraphState(TypedDict, total=False):
    sub: Subsystem
    prd_file_id: str
    current_spec: str
    best_spec: str
    best_score: int
    used_chars: int
    attempts: list
    iteration: int
    feedback: str
    # 注入的固定上下文（SpecContext 拆出来的）
    ctx_backend: Any
    ctx_llm: Any
    ctx_state: dict
    ctx_settings: Any
    ctx_vector_store: Any
    ctx_critic_llm: Any
```

- [ ] **Step 1: 写子图节点函数**

Create `backend/src/porto_chatbot/specs/subgraph.py`:
```python
"""单个 subsystem 的 evaluator-optimizer 子图（四重终止）。"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ..models import SpecAttempt
from ..models.enums import SpecVerdict
from .context import SpecContext
from .steps import critique_spec, generate_initial_spec, refine_spec
from .template import render_template_spec

_FEEDBACK_DIGEST_MAX = 200


class SpecSubgraphState(TypedDict, total=False):
    sub: Any
    prd_file_id: str
    current_spec: str
    best_spec: str
    best_score: int
    used_chars: int
    attempts: list
    iteration: int
    feedback: str
    ctx_backend: Any
    ctx_llm: Any
    ctx_state: dict
    ctx_settings: Any
    ctx_vector_store: Any
    ctx_critic_llm: Any


def _ctx(state: SpecSubgraphState) -> SpecContext:
    return SpecContext(backend=state["ctx_backend"], llm=state["ctx_llm"],
                       state=state.get("ctx_state", {}), settings=state["ctx_settings"],
                       vector_store=state.get("ctx_vector_store"), critic_llm=state.get("ctx_critic_llm"))


def init_spec(state: SpecSubgraphState) -> dict:
    ctx = _ctx(state)
    sub = state["sub"]
    if not (ctx.llm.enabled and ctx.settings.spec_refine_enabled):
        return {"current_spec": render_template_spec(ctx, sub), "best_spec": render_template_spec(ctx, sub),
                "best_score": -1, "attempts": [], "iteration": 0, "used_chars": 0}
    spec, _ = generate_initial_spec(ctx, sub)
    if not spec:
        spec = render_template_spec(ctx, sub)
    return {"current_spec": spec, "best_spec": spec, "best_score": -1, "attempts": [], "iteration": 0,
            "used_chars": len(spec)}


def critique_node(state: SpecSubgraphState) -> dict:
    ctx = _ctx(state)
    sub = state["sub"]
    i = state.get("iteration", 0) + 1
    spec = state["current_spec"]
    critique = critique_spec(ctx, sub, spec)
    if critique is None:
        return {"iteration": i, "attempts": state.get("attempts", []) + [
            SpecAttempt(version=i, verdict=SpecVerdict.NEEDS_IMPROVEMENT, feedback_digest="critic 不可用")]}
    attempts = state.get("attempts", []) + [SpecAttempt(
        version=i, score=critique.score, verdict=critique.verdict,
        feedback_digest=critique.feedback[:_FEEDBACK_DIGEST_MAX])]
    used = state.get("used_chars", 0) + len(critique.feedback)
    best_score = state.get("best_score", -1)
    best_spec = state["best_spec"]
    if critique.score > best_score:
        best_spec, best_score = spec, critique.score
    return {"iteration": i, "attempts": attempts, "used_chars": used,
            "best_spec": best_spec, "best_score": best_score, "feedback": critique.feedback}


def refine_node(state: SpecSubgraphState) -> dict:
    ctx = _ctx(state)
    refined = refine_spec(ctx, state["sub"], state["current_spec"], state.get("feedback", ""))
    if refined and refined.strip():
        return {"current_spec": refined, "used_chars": state.get("used_chars", 0) + len(refined)}
    return {}


def finalize_node(state: SpecSubgraphState) -> dict:
    from ..models import SpecResult
    best = state.get("best_spec") or state.get("current_spec", "")
    sub = state["sub"]
    attempts = state.get("attempts", [])
    return {"spec_results": {sub.name: SpecResult(
        final=best, attempts=attempts, iterations=len(attempts),
        truncated=state.get("used_chars", 0) > ctx_budget(state) or state.get("iteration", 0) >= max_iter(state),
        used_llm=True, tool_meta={})}}


def ctx_budget(state: SpecSubgraphState) -> int:
    return state["ctx_settings"].spec_refine_budget_tokens * 4


def max_iter(state: SpecSubgraphState) -> int:
    return state["ctx_settings"].spec_refine_max_iter


def pass_score(state: SpecSubgraphState) -> int:
    return state["ctx_settings"].spec_refine_pass_score


def _should_stop(state: SpecSubgraphState) -> str:
    """四重终止：返回 'finalize' 或 'refine'。"""
    i = state.get("iteration", 0)
    if not state.get("attempts"):
        return "finalize"
    last = state["attempts"][-1]
    # ① 达标
    if last.verdict == SpecVerdict.PASS or last.score >= pass_score(state):
        return "finalize"
    # ③ 分数退化（震荡）
    if state.get("best_score", -1) >= 0 and last.score < state["best_score"]:
        return "finalize"
    # ② 迭代上限
    if i >= max_iter(state):
        return "finalize"
    # ④ 预算
    if state.get("used_chars", 0) > ctx_budget(state):
        return "finalize"
    return "refine"


def build_spec_subgraph():
    g = StateGraph(SpecSubgraphState)
    g.add_node("init_spec", init_spec)
    g.add_node("critique", critique_node)
    g.add_node("refine", refine_node)
    g.add_node("finalize", finalize_node)
    g.add_edge(START, "init_spec")
    g.add_edge("init_spec", "critique")
    g.add_conditional_edges("critique", _should_stop, {"finalize": "finalize", "refine": "refine"})
    g.add_edge("refine", "critique")
    g.add_edge("finalize", END)
    return g.compile()
```

- [ ] **Step 2: 写测试 — 四重终止**

Create `backend/tests/test_spec_subgraph.py`，用 mock SpecContext 测试 `_should_stop` 的 4 个分支（达标/退化/上限/预算）+ init 的 LLM-disabled 降级路径。

- [ ] **Step 3: 运行测试 + Commit**

Run: `cd backend && uv run pytest tests/test_spec_subgraph.py -v`
```bash
git add backend/src/porto_chatbot/specs/subgraph.py backend/tests/test_spec_subgraph.py
git commit -m "feat(specs): add evaluator-optimizer subgraph with 4-way termination"
```

---

### Task 9: graph.py — 删 evaluate + Send fan-out + Semaphore 限流

**Files:**
- Modify: `backend/src/porto_chatbot/agent/graph.py`
- Modify: `backend/src/porto_chatbot/agent/nodes/generate.py`（改为 Send 派发节点）

**Interfaces:**
- Consumes: `build_spec_subgraph()`（Task 8）、`PortoAgentState`
- Produces: 新 graph 拓扑 `retrieve→understand→identify→[Send spec_subgraph ×N]→END`

- [ ] **Step 1: 改 generate 节点为 Send 派发**

`generate.py` 删除 `ThreadPoolExecutor` 块（:29-40），改为返回 Send 列表的派发逻辑。在 graph.py 用 conditional_edges 消费。

In `agent/nodes/generate.py` 替换为：
```python
from __future__ import annotations

from langgraph.types import Send


def dispatch_specs(state, *, config):
    """identify 之后的派发：每个 subsystem 一个 Send 到 spec_subgraph。"""
    agent = config["configurable"]["agent"]
    base = {"ctx_backend": agent.backend, "ctx_llm": agent.llm,
            "ctx_state": {**state}, "ctx_settings": agent.settings,
            "ctx_vector_store": agent.vector_store, "ctx_critic_llm": agent.critic_llm,
            "prd_file_id": state.get("prd_file_id")}
    return [Send("spec_subgraph", {"sub": sub, **base}) for sub in state["subsystems"]]
```

- [ ] **Step 2: 改 graph.py 拓扑**

In `agent/graph.py`，`build_workflow_graph` 改为：
```python
from langgraph.types import Send
from ..specs.subgraph import build_spec_subgraph

def build_workflow_graph(checkpointer):
    g = StateGraph(PortoAgentState)
    g.add_node("retrieve", retrieve_node.retrieve_knowledge)
    g.add_node("understand", understand_node.understand_prd)
    g.add_node("identify", identify_node.identify_subsystems)
    g.add_node("spec_subgraph", build_spec_subgraph())  # 编译后的子图作为节点
    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "understand")
    g.add_edge("understand", "identify")
    # Send fan-out：identify → 每个 subsystem 一份 spec_subgraph
    g.add_conditional_edges("identify", generate_node.dispatch_specs, ["spec_subgraph"])
    g.add_edge("spec_subgraph", END)
    return g.compile(checkpointer=checkpointer, interrupt_after=["understand", "identify"])
```
删除 `evaluate` 节点、`generate` 节点（被 spec_subgraph 替代）、evaluate→identify 回边。

- [ ] **Step 3: 并发限流（Semaphore）**

Send 默认全并发。用 `config["recursion_limit"]` 或在 spec_subgraph 节点外包一层并发闸。最简方案：在 `dispatch_specs` 外、graph compile 前无法直接限流 Send——改为在 `_run_tool`/execute_node 层加全局 Semaphore，或在 spec_subgraph 的 init_spec 节点入口 acquire 一个 settings 级 Semaphore（`agent.settings.spec_refine_concurrency`）。实现时用模块级 `threading.Semaphore`：
```python
# specs/subgraph.py 顶部
import threading
_spec_semaphore: threading.Semaphore | None = None

def _init_semaphore(max_concurrent: int):
    global _spec_semaphore
    _spec_semaphore = threading.Semaphore(max_concurrent)
```
在 `init_spec` 入口 `_spec_semaphore.acquire()` / finally release（子图内部串行，但多个子图实例并发 → 信号量限实例数）。graph 编译前调 `_init_semaphore(settings.spec_refine_concurrency)`。

- [ ] **Step 4: 编译 + Commit**

Run: `cd backend && uv run python -c "from porto_chatbot.agent.graph import build_workflow_graph; print('ok')"`
```bash
git add backend/src/porto_chatbot/agent/graph.py backend/src/porto_chatbot/agent/nodes/generate.py backend/src/porto_chatbot/specs/subgraph.py
git commit -m "feat(graph): Send fan-out spec subgraph, remove evaluate node"
```

---

### Task 10: 清理 evaluate + rework 残留

**Files:**
- Delete: `backend/src/porto_chatbot/agent/nodes/evaluate.py`
- Modify: `backend/src/porto_chatbot/agent/nodes/__init__.py`（移除 evaluate 导出）
- Modify: `backend/src/porto_chatbot/evaluation.py`（若仅 evaluate 用，保留但不再被 graph 调用）
- Modify: `backend/src/porto_chatbot/agent/state.py`（删 `evaluation`/`needs_rework`/`rework_passes` 字段）
- Modify: 全局搜 `needs_rework` / `rework_passes` / `evaluate` 引用

**Interfaces:**
- 无新增，纯清理

- [ ] **Step 1: 搜残留引用**

Run: `cd backend && grep -rn 'needs_rework\|rework_passes\|workflow_rework' src/porto_chatbot/`
逐个清理（settings 的 `workflow_rework_enabled`/`workflow_rework_max_passes` 可保留为废弃字段或一并删——删则同步前端 AgentConfig + config_store 白名单）。

- [ ] **Step 2: 删 evaluate.py**

删 `agent/nodes/evaluate.py`，改 `agent/nodes/__init__.py` 移除导出，改 `graph.py` 的 STEPS/_NODE_FNS 移除 evaluate（Task 9 已改 graph，这里清 import）。

- [ ] **Step 3: 清 state 字段**

`state.py` 删 `evaluation`/`needs_rework`/`rework_passes`（确认无引用后）。

- [ ] **Step 4: 运行测试 + Commit**

Run: `cd backend && uv run pytest tests/ -x`
```bash
git add -A backend/src/porto_chatbot/
git commit -m "refactor: remove evaluate node and rework logic"
```

---

## Phase 4 — chatbot 上传

### Task 11: 后端 /api/chat/files + ChatRequest.file_ids

**Files:**
- Modify: `backend/src/porto_chatbot/api/routes/chat.py`
- Modify: `backend/src/porto_chatbot/models/chat.py`（ChatRequest 加 file_ids）
- Modify: `backend/src/porto_chatbot/api/sse.py`（`_chat_request_from_stream_body` 解析 file_ids）

**Interfaces:**
- Produces: `POST /api/chat/files`、`ChatRequest.file_ids: list[str]`

- [ ] **Step 1: ChatRequest 加 file_ids**

In `backend/src/porto_chatbot/models/chat.py`，`ChatRequest`（:18-24）加：
```python
file_ids: list[str] = Field(default_factory=list)
```

- [ ] **Step 2: 加上传端点**

In `backend/src/porto_chatbot/api/routes/chat.py`，追加：
```python
from fastapi import UploadFile, File
from ..files.service import FileService

@router.post("/api/chat/files")
async def upload_chat_file(file: Annotated[UploadFile, File()],
                           session_id: Annotated[str, Form()] = "default"):
    svc = FileService(apply_rag_settings())  # 或注入单例
    meta = svc.store(file, owner_id=session_id)
    return {"file_id": meta.file_id, "page_count": meta.page_count, "original_name": meta.original_name}
```

- [ ] **Step 3: stream body 解析 file_ids**

In `backend/src/porto_chatbot/api/sse.py`，`_chat_request_from_stream_body` 把 body 里的 `file_ids` 传进 ChatRequest。

- [ ] **Step 4: 编译 + Commit**

Run: `cd backend && uv run python -c "from porto_chatbot.api.routes.chat import router; print('ok')"`
```bash
git add backend/src/porto_chatbot/api/routes/chat.py backend/src/porto_chatbot/models/chat.py backend/src/porto_chatbot/api/sse.py
git commit -m "feat(chat): add /api/chat/files upload + ChatRequest.file_ids"
```

---

### Task 12: agent_sdk chat 路径注入 file_service + read_file MCP tool

**Files:**
- Modify: `backend/src/porto_chatbot/agent_sdk/backend.py`（`_build_chat_options` 注入 file_service 到 ctx，:315-477）
- Modify: `backend/src/porto_chatbot/agent_sdk/tools.py`（chatbot 工具集追加 read_file 组，已在 Task 4 实现，这里确保 chat 路径注册）

**Interfaces:**
- Consumes: `ChatRequest.file_ids`、`FileService`

- [ ] **Step 1: _build_chat_options 注入 file_service**

`agent_sdk/backend.py` 的 `_build_chat_options`（:315）构造 `AgentToolContext` 时，补 `file_service=...`，并把 `req.file_ids` 写进 system prompt 提示（告诉 Claude 可用 file_id 列表）。

- [ ] **Step 2: system prompt 提示可用文件**

在 chat 的 system prompt 组装处，若 `req.file_ids` 非空，追加：
```
本次对话关联了以下文件，可调用 get_file_info / read_file_pages / search_file 读取：{file_ids}
```

- [ ] **Step 3: 编译 + Commit**

Run: `cd backend && uv run pytest tests/ -x -k chat`
```bash
git add backend/src/porto_chatbot/agent_sdk/backend.py
git commit -m "feat(chat): inject file_service + file_ids into agent sdk chat"
```

---

### Task 13: 前端聊天附件 UI + transport body

**Files:**
- Modify: `frontend/src/components/porto-workbench.tsx`（Composer :1071-1088、ChatSession transport :800-828）
- Modify: `frontend/src/lib/api.ts`（加 `uploadChatFile`）

**Interfaces:**
- Consumes: `POST /api/chat/files`

- [ ] **Step 1: api.ts 加 uploadChatFile**

In `frontend/src/lib/api.ts`，追加（仿 `createWorkflowUpload` :99-112）：
```ts
export async function uploadChatFile(
  file: File, sessionId: string,
): Promise<{ file_id: string; page_count: number; original_name: string }> {
  const form = new FormData();
  form.set("file", file);
  form.set("session_id", sessionId);
  return parseJson(await fetch("/api/chat/files", { method: "POST", body: form }));
}
```

- [ ] **Step 2: Composer 加附件按钮 + state**

`porto-workbench.tsx` 的 `Composer`（:1071）改造：加一个隐藏 `<input type="file" multiple accept=".pdf,.docx,.md,.txt">` + Upload 图标按钮，已选文件名显示在输入框上方。附件状态提升到 `ChatSession`（传给 Composer + transport body）。

- [ ] **Step 3: transport body 带 file_ids**

`ChatSession`（:800-828）的 `AssistantChatTransport` body 追加 `file_ids: selectedFileIds`，发送后清空已选。

- [ ] **Step 4: build 验证 + Commit**

Run: `cd frontend && npm run build`
```bash
git add frontend/src/components/porto-workbench.tsx frontend/src/lib/api.ts
git commit -m "feat(chat-ui): add attachment upload to chat composer"
```

---

## Phase 5 — 并发配置 + Mermaid

### Task 14: spec_refine_concurrency 默认 3→4

**Files:**
- Modify: `backend/src/porto_chatbot/settings.py:82`
- Modify: `frontend/src/lib/api.ts:254`

**Interfaces:**
- 无新增，改默认值

- [ ] **Step 1: 后端默认值**

`settings.py:82`：`spec_refine_concurrency: int = Field(default=4, ge=1, le=10)`

- [ ] **Step 2: 前端默认值**

`api.ts:254`：`spec_refine_concurrency: 4,`

- [ ] **Step 3: Commit**

```bash
git add backend/src/porto_chatbot/settings.py frontend/src/lib/api.ts
git commit -m "chore: bump spec_refine_concurrency default to 4"
```

---

### Task 15: architecture-view.tsx 更新 LANGGRAPH + 新增 FILE_SERVICE 图

**Files:**
- Modify: `frontend/src/components/architecture-view.tsx`

**Interfaces:**
- 无

- [ ] **Step 1: 更新 LANGGRAPH 图**

`architecture-view.tsx:65-72` 替换为：
```ts
const LANGGRAPH = `stateDiagram-v2
    [*] --> retrieve: prd_file_id (pointer)
    retrieve --> understand: + sources
    understand --> identify: + understanding
    identify --> spec_subgraph: Send fan-out (并发=spec_refine_concurrency)
    spec_subgraph --> [*]: 各 spec 独立交付（无 evaluate）`;
```
同步改 `DiagramSection` 的 description（:155-159）去掉 evaluate/needs_rework 描述。

- [ ] **Step 2: 新增 FILE_SERVICE 图**

在 `architecture-view.tsx` 加常量 + 在 "Context 组装" 区或新区挂载：
```ts
const FILE_SERVICE = `flowchart TB
    U[用户上传] --> S[FileService.store]
    S -->|落盘| D[data_dir/files]
    S -->|元数据| DB[(files.sqlite3)]
    S -->|分页提取| P[pages_json]
    P --> RP[read_file_pages]
    P --> SE[search_file]
    GI[get_file_info] --> DB
    RP --> A[workflow 节点 / chatbot agent 按需读取]
    SE --> A`;
```
在 `ArchitectureView` 组件加一个 `<DiagramSection title="文件服务（Memory Pointer）" chart={FILE_SERVICE} ... />`。

- [ ] **Step 3: build 验证 + Commit**

Run: `cd frontend && npm run build`
```bash
git add frontend/src/components/architecture-view.tsx
git commit -m "docs(arch): update LANGGRAPH diagram + add FILE_SERVICE diagram"
```

---

## 收尾

### Task 16: 端到端冒烟 + 全量测试

- [ ] **Step 1: 后端全量测试**

Run: `cd backend && uv run pytest tests/ -x`

- [ ] **Step 2: 前端 build**

Run: `cd frontend && npm run build`

- [ ] **Step 3: 手动冒烟（workflow + chatbot 两场景）**

启动后端 + 前端，上传一个 PRD 文件跑 workflow（验证 retrieve→understand→identify→spec 子图→各 spec 独立产出，无 evaluate）；chatbot 上传文件提问（验证 read_file tool 被调用）。

- [ ] **Step 4: 最终 Commit**

```bash
git add -A
git commit -m "test: end-to-end smoke validation"
```
