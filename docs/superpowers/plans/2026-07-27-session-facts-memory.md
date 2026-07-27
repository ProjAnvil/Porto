# Session Facts Memory 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 Agent Memory 层补齐 long-term key facts 档:每轮异步 LLM 提取结构化事实 → sqlite 存储 → 按优先级注入 system prompt;同时强化 compaction 摘要 prompt 保留实体。

**Architecture:** 新增 `memory/facts.py`(SessionFactsStore + extract/build/trigger)+ `session_facts` sqlite 表 + `SessionFact` model。提取走 δ-1 异步触发(流式 `asyncio.create_task` + `asyncio.to_thread`、非流式 `threading.Thread`),复用 `LLMClient.complete_structured` 做 JSON 输出。注入插在 prompt_parts 的"用户问题"之后(最高优先级),接进现有 `_trim_to_budget`。

**Tech Stack:** Python 3.12 / FastAPI / pydantic / sqlite3 / LLMClient(langchain)| 无新依赖。

## Global Constraints

- Python ≥ 3.12,`uv sync` 已装的依赖,不引入新包
- 测试框架:`pytest` + `pytest-asyncio`(已在 dev 依赖)
- 测试目录:`backend/tests/`,新建 `backend/tests/memory/` 子目录
- sqlite 路径:`settings.memory_db_path`(复用现有,不新建 db)
- LLM 调用:`LLMClient.complete_structured(system, user, schema_hint: dict) -> dict | None`(自带 JSON 解析+重试,见 `llm/client.py:157`)
- 分词:`embeddings.tokens(text)`(CJK 单字+bigram,见 `embeddings.py:16`),Jaccard 模糊匹配复用
- 风格:`from __future__ import annotations`、pydantic BaseModel、`get_component_logger` 日志
- 所有 facts 操作 fail-open,不阻塞主 chat 链路
- 提交信息前缀:`feat(memory):` / `test(memory):` / `refactor(memory):`,Co-Authored-By Claude

---

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `backend/src/porto_chatbot/models/chat.py` | `SessionFact` pydantic model | 改(加类) |
| `backend/src/porto_chatbot/models/__init__.py` | 导出 `SessionFact` | 改 |
| `backend/src/porto_chatbot/settings.py` | `facts_*` BaseSettings 字段 | 改 |
| `backend/src/porto_chatbot/models/payload.py` | `AgentSettingsPayload` 加 `facts_*` override | 改 |
| `backend/src/porto_chatbot/memory/store.py` | `_init_db` 加 `session_facts` 表 migration | 改 |
| `backend/src/porto_chatbot/memory/facts.py` | SessionFactsStore + extract/build/trigger | **新建** |
| `backend/src/porto_chatbot/memory/__init__.py` | 导出 facts 公开 API | 改 |
| `backend/src/porto_chatbot/memory/compaction.py` | a1 实体感知摘要 prompt | 改 |
| `backend/src/porto_chatbot/api/routes/chat.py` | 两路注入 facts + 异步触发 | 改 |
| `backend/src/porto_chatbot/api/routes/memory.py` | GET `/api/memory/{sid}/facts` | 改 |
| `backend/tests/memory/__init__.py` | 测试包 | **新建** |
| `backend/tests/memory/test_facts_store.py` | SessionFactsStore 单测 | **新建** |
| `backend/tests/memory/test_facts_extract.py` | extract/build/trigger 单测 | **新建** |
| `backend/tests/memory/test_compaction.py` | a1 摘要 prompt 单测 | **新建** |
| `backend/tests/api/test_chat_facts.py` | chat 注入集成测 | **新建** |
| `backend/tests/api/test_memory_facts_api.py` | facts API 单测 | **新建** |

---

### Task 1: SessionFact model + settings 配置

**Files:**
- Modify: `backend/src/porto_chatbot/models/chat.py`(末尾加 `SessionFact`)
- Modify: `backend/src/porto_chatbot/models/__init__.py`(导出)
- Modify: `backend/src/porto_chatbot/settings.py`(加 `facts_*` 字段,在 `memory_recent_keep` 之后)
- Modify: `backend/src/porto_chatbot/models/payload.py`(加 `facts_*` 到 `AgentSettingsPayload`)
- Test: `backend/tests/memory/__init__.py`(空文件) + `backend/tests/memory/test_models.py`

**Interfaces:**
- Produces: `SessionFact` model;`Settings.facts_enabled` / `facts_max_per_category` / `facts_similarity_threshold` / `facts_recent_context_turns` / `facts_provider` / `facts_model`

- [ ] **Step 1: Write the failing test**

`backend/tests/memory/__init__.py`(空文件,建立包):
```python
```

`backend/tests/memory/test_models.py`:
```python
from __future__ import annotations

from porto_chatbot.models import SessionFact
from porto_chatbot.settings import Settings


def test_session_fact_defaults():
    fact = SessionFact(
        id="f1",
        session_id="s1",
        category="user_decision",
        content="登录采用 OAuth",
        source_msg_id="m1",
        created_at="2026-07-27T00:00:00Z",
        updated_at="2026-07-27T00:00:00Z",
    )
    assert fact.status == "active"
    assert fact.category == "user_decision"


def test_settings_facts_defaults():
    s = Settings()
    assert s.facts_enabled is True
    assert s.facts_max_per_category == 20
    assert s.facts_similarity_threshold == 0.5
    assert s.facts_recent_context_turns == 6
    assert s.facts_provider is None
    assert s.facts_model is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/memory/test_models.py -v`
Expected: FAIL with `ImportError: cannot import name 'SessionFact'`

- [ ] **Step 3: Add `SessionFact` to `models/chat.py`**

在 `MemorySearchResponse` 之后追加:
```python
class SessionFact(BaseModel):
    """会话级关键事实(a2)。每条独立原子化,带 category 优先级与 status 生命周期。"""

    id: str
    session_id: str
    category: Literal["user_decision", "user_preference", "project_context", "open_question"]
    content: str
    status: Literal["active", "retracted"] = "active"
    source_msg_id: str | None = None
    created_at: str
    updated_at: str
```

- [ ] **Step 4: Export from `models/__init__.py`**

在 `from .chat import (...)` 块里加 `SessionFact`:
```python
from .chat import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    MemoryRecord,
    MemorySearchResponse,
    SessionFact,
)
```

- [ ] **Step 5: Add `facts_*` to `settings.py`**

在 `memory_recent_keep` 字段之后(`context_char_budget` 之前)插入:
```python
    # Session facts (a2 long-term key facts)
    facts_enabled: bool = True
    facts_max_per_category: int = Field(default=20, ge=1, le=100)
    facts_similarity_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    facts_recent_context_turns: int = Field(default=6, ge=1, le=20)
    facts_provider: Literal["openai", "anthropic"] | None = None
    facts_model: str | None = None
```

- [ ] **Step 6: Add `facts_*` overrides to `AgentSettingsPayload`** (`models/payload.py`)

在 `memory_recent_keep` 字段之后插入:
```python
    # Session facts (a2)
    facts_enabled: bool | None = None
    facts_max_per_category: int | None = Field(default=None, ge=1, le=100)
    facts_similarity_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    facts_recent_context_turns: int | None = Field(default=None, ge=1, le=20)
```

(注:`facts_provider`/`facts_model` 不暴露到 payload override,保持内部配置。)

- [ ] **Step 7: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/memory/test_models.py -v`
Expected: PASS (2 tests)

- [ ] **Step 8: Commit**

```bash
git add backend/src/porto_chatbot/models/ backend/src/porto_chatbot/settings.py backend/tests/memory/__init__.py backend/tests/memory/test_models.py
git commit -m "$(cat <<'EOF'
feat(memory): add SessionFact model + facts_* settings

为 a2 关键事实层打基础:SessionFact pydantic model(category/status/
source_msg_id)+ Settings.facts_* 配置(上限/阈值/最近轮数/独立 LLM)。

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: session_facts 表 migration

**Files:**
- Modify: `backend/src/porto_chatbot/memory/store.py:_init_db`(加表 + 索引)
- Test: `backend/tests/memory/test_store_facts_migration.py`

**Interfaces:**
- Consumes: `Settings.memory_db_path`
- Produces: `session_facts` 表 + `idx_facts_session` / `idx_facts_session_cat` 索引

- [ ] **Step 1: Write the failing test**

`backend/tests/memory/test_store_facts_migration.py`:
```python
from __future__ import annotations

import sqlite3

from porto_chatbot.settings import Settings


def test_session_facts_table_created(tmp_path):
    settings = Settings(data_dir=tmp_path)
    # 触发 _init_db(MemoryStore.__init__ 会调)
    from porto_chatbot.memory.store import MemoryStore

    MemoryStore(settings)
    with sqlite3.connect(settings.memory_db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(session_facts)").fetchall()}
        assert cols == {
            "id", "session_id", "category", "content",
            "status", "source_msg_id", "created_at", "updated_at",
        }
        idx = {row[1] for row in conn.execute("PRAGMA index_list(session_facts)").fetchall()}
        assert "idx_facts_session" in idx
        assert "idx_facts_session_cat" in idx
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/memory/test_store_facts_migration.py -v`
Expected: FAIL with `sqlite3.OperationalError: no such table: session_facts`

- [ ] **Step 3: Add migration to `store.py:_init_db`**

在 `_init_db` 方法末尾(`session_summaries` 表 CREATE 之后)追加:
```python
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_facts (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    source_msg_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_facts_session ON session_facts(session_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_facts_session_cat "
                "ON session_facts(session_id, category)"
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/memory/test_store_facts_migration.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/memory/store.py backend/tests/memory/test_store_facts_migration.py
git commit -m "$(cat <<'EOF'
feat(memory): add session_facts table migration

在 _init_db 加 session_facts 表(id/session_id/category/content/status/
source_msg_id/created_at/updated_at)+ session 与 session+category 索引。

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: SessionFactsStore — upsert(模糊匹配 + 上限淘汰)

**Files:**
- Create: `backend/src/porto_chatbot/memory/facts.py`
- Test: `backend/tests/memory/test_facts_store.py`

**Interfaces:**
- Consumes: `Settings`(memory_db_path / facts_max_per_category / facts_similarity_threshold);`embeddings.tokens`
- Produces: `SessionFactsStore.upsert(*, session_id, category, content, source_msg_id) -> str`

- [ ] **Step 1: Write the failing test**

`backend/tests/memory/test_facts_store.py`:
```python
from __future__ import annotations

import pytest

from porto_chatbot.settings import Settings


@pytest.fixture
def store(tmp_path):
    from porto_chatbot.memory.store import MemoryStore
    from porto_chatbot.memory.facts import SessionFactsStore

    settings = Settings(data_dir=tmp_path)
    MemoryStore(settings)  # 触发 _init_db
    return SessionFactsStore(settings)


def test_upsert_new_fact(store):
    fid = store.upsert(
        session_id="s1", category="user_decision",
        content="登录采用 OAuth", source_msg_id="m1",
    )
    assert fid
    active = store.list_active("s1")
    assert len(active) == 1
    assert active[0].content == "登录采用 OAuth"
    assert active[0].status == "active"


def test_upsert_updates_when_similar(store):
    store.upsert(session_id="s1", category="user_decision",
                 content="登录采用 OAuth", source_msg_id="m1")
    store.upsert(session_id="s1", category="user_decision",
                 content="登录采用 OAuth 方式", source_msg_id="m2")  # Jaccard ≥ 0.5
    active = store.list_active("s1")
    assert len(active) == 1  # 不是新增,是更新
    assert "OAuth 方式" in active[0].content


def test_upsert_adds_when_dissimilar(store):
    store.upsert(session_id="s1", category="user_decision",
                 content="登录采用 OAuth", source_msg_id="m1")
    store.upsert(session_id="s1", category="user_decision",
                 content="前端用 React 做表格组件", source_msg_id="m2")  # 无重叠
    active = store.list_active("s1")
    assert len(active) == 2


def test_upsert_evicts_oldest_when_category_full(store):
    store.settings.facts_max_per_category = 3
    for i in range(4):
        store.upsert(session_id="s1", category="user_preference",
                     content=f"偏好编号 {i} 各不相同", source_msg_id=f"m{i}")
    active = store.list_active("s1")
    assert len(active) == 3
    contents = [f.content for f in active]
    assert "偏好编号 0 各不相同" not in contents  # 最旧的被淘汰
    assert "偏好编号 3 各不相同" in contents


def test_upsert_scoped_by_session_category(store):
    store.upsert(session_id="s1", category="user_decision",
                 content="登录采用 OAuth", source_msg_id="m1")
    store.upsert(session_id="s2", category="user_decision",
                 content="登录采用 OAuth", source_msg_id="m1")  # 同内容不同 session
    assert len(store.list_active("s1")) == 1
    assert len(store.list_active("s2")) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/memory/test_facts_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'porto_chatbot.memory.facts'`

- [ ] **Step 3: Create `memory/facts.py` with `SessionFactsStore.upsert`**

```python
from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime

from ..embeddings import tokens
from ..logging_utils import get_component_logger
from ..models import SessionFact
from ..settings import Settings

_CATEGORY_PRIORITY = {
    "user_decision": 0,
    "user_preference": 1,
    "project_context": 2,
    "open_question": 3,
}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


class SessionFactsStore:
    """session_facts 表的 CRUD 封装。upsert 用 token Jaccard 模糊匹配去重。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = get_component_logger("facts", settings)

    def upsert(
        self, *, session_id: str, category: str, content: str,
        source_msg_id: str | None,
    ) -> str:
        now = datetime.now(UTC).isoformat()
        new_tokens = set(tokens(content))
        with sqlite3.connect(self.settings.memory_db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, content FROM session_facts "
                "WHERE session_id=? AND category=? AND status='active'",
                (session_id, category),
            ).fetchall()
            threshold = self.settings.facts_similarity_threshold
            for row in rows:
                existing_tokens = set(tokens(row["content"]))
                if _jaccard(new_tokens, existing_tokens) >= threshold:
                    conn.execute(
                        "UPDATE session_facts SET content=?, source_msg_id=?, updated_at=? "
                        "WHERE id=?",
                        (content, source_msg_id, now, row["id"]),
                    )
                    self.logger.info(
                        "facts upsert update id=%s session=%s category=%s",
                        row["id"], session_id, category,
                    )
                    return row["id"]
            fact_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO session_facts "
                "(id, session_id, category, content, status, source_msg_id, "
                " created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
                (fact_id, session_id, category, content, source_msg_id, now, now),
            )
            self._enforce_cap(conn, session_id, category)
            self.logger.info(
                "facts upsert insert id=%s session=%s category=%s",
                fact_id, session_id, category,
            )
            return fact_id

    def _enforce_cap(self, conn, session_id: str, category: str) -> None:
        """超 facts_max_per_category 时,按 updated_at 淘汰最旧的 active fact。"""
        cap = self.settings.facts_max_per_category
        count = conn.execute(
            "SELECT COUNT(*) FROM session_facts "
            "WHERE session_id=? AND category=? AND status='active'",
            (session_id, category),
        ).fetchone()[0]
        if count <= cap:
            return
        to_delete = count - cap
        stale = conn.execute(
            "SELECT id FROM session_facts "
            "WHERE session_id=? AND category=? AND status='active' "
            "ORDER BY updated_at ASC LIMIT ?",
            (session_id, category, to_delete),
        ).fetchall()
        for row in stale:
            conn.execute("DELETE FROM session_facts WHERE id=?", (row["id"],))
        self.logger.info(
            "facts cap evicted session=%s category=%s count=%s", session_id, category, to_delete,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/memory/test_facts_store.py -v`
Expected: PASS (5 tests)

> 注:`list_active` 在 Task 4 实现。本任务的测试调用 `list_active` 会失败 —— **先把 `list_active` 加为最小桩**:
> ```python
>     def list_active(self, session_id: str) -> list[SessionFact]:
>         with sqlite3.connect(self.settings.memory_db_path) as conn:
>             conn.row_factory = sqlite3.Row
>             rows = conn.execute(
>                 "SELECT * FROM session_facts WHERE session_id=? AND status='active' "
>                 "ORDER BY updated_at DESC",
>                 (session_id,),
>             ).fetchall()
>         return [self._row_to_fact(r) for r in rows]
>
>     def _row_to_fact(self, row: sqlite3.Row) -> SessionFact:
>         return SessionFact(
>             id=row["id"], session_id=row["session_id"], category=row["category"],
>             content=row["content"], status=row["status"],
>             source_msg_id=row["source_msg_id"],
>             created_at=row["created_at"], updated_at=row["updated_at"],
>         )
> ```
> (Task 4 会扩展 `list_active` 加 category 优先级排序 + 加 `by_category` / `retract`。)

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/memory/facts.py backend/tests/memory/test_facts_store.py
git commit -m "$(cat <<'EOF'
feat(memory): SessionFactsStore.upsert with Jaccard fuzzy match

upsert 用 embeddings.tokens 算 Jaccard,≥ facts_similarity_threshold 则
更新同条,否则新增;超 facts_max_per_category 按 updated_at 淘汰最旧。

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: SessionFactsStore — retract / list_active / by_category

**Files:**
- Modify: `backend/src/porto_chatbot/memory/facts.py`
- Modify: `backend/tests/memory/test_facts_store.py`(追加测试)

**Interfaces:**
- Produces: `SessionFactsStore.retract(fact_id) -> None`;`list_active(session_id) -> list[SessionFact]`(按 category 优先级);`by_category(session_id) -> dict[str, list[SessionFact]]`

- [ ] **Step 1: Write the failing tests** (追加到 `test_facts_store.py`)

```python
def test_retract_marks_status(store):
    fid = store.upsert(session_id="s1", category="user_decision",
                       content="登录采用 OAuth", source_msg_id="m1")
    store.retract(fid)
    active = store.list_active("s1")
    assert len(active) == 0  # retracted 的不进 active


def test_retract_idempotent(store):
    fid = store.upsert(session_id="s1", category="user_decision",
                       content="登录采用 OAuth", source_msg_id="m1")
    store.retract(fid)
    store.retract(fid)  # 不报错


def test_list_active_orders_by_category_priority(store):
    store.upsert(session_id="s1", category="open_question",
                 content="前端框架未定", source_msg_id="m1")
    store.upsert(session_id="s1", category="user_decision",
                 content="登录采用 OAuth", source_msg_id="m2")
    store.upsert(session_id="s1", category="user_preference",
                 content="后端用 Go", source_msg_id="m3")
    active = store.list_active("s1")
    assert [f.category for f in active] == [
        "user_decision", "user_preference", "open_question",
    ]


def test_by_category_groups(store):
    store.upsert(session_id="s1", category="user_decision",
                 content="登录采用 OAuth", source_msg_id="m1")
    store.upsert(session_id="s1", category="user_decision",
                 content="不用 SAML", source_msg_id="m2")
    store.upsert(session_id="s1", category="project_context",
                 content="金融客户项目", source_msg_id="m3")
    grouped = store.by_category("s1")
    assert len(grouped["user_decision"]) == 2
    assert len(grouped["project_context"]) == 1
    assert "open_question" not in grouped  # 空组不出现
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/memory/test_facts_store.py -v`
Expected: 4 new tests FAIL(`retract` 不存在、`list_active` 无优先级排序、`by_category` 不存在)

- [ ] **Step 3: Implement `retract` + 重写 `list_active` + 加 `by_category`**

在 `SessionFactsStore` 里(Task 3 的桩 `list_active` 替换为下面版本):
```python
    def retract(self, fact_id: str) -> None:
        with sqlite3.connect(self.settings.memory_db_path) as conn:
            conn.execute(
                "UPDATE session_facts SET status='retracted' WHERE id=?",
                (fact_id,),
            )
        self.logger.info("facts retract id=%s", fact_id)

    def list_active(self, session_id: str) -> list[SessionFact]:
        grouped = self.by_category(session_id)
        ordered: list[SessionFact] = []
        for cat in sorted(grouped, key=lambda c: _CATEGORY_PRIORITY.get(c, 99)):
            ordered.extend(grouped[cat])
        return ordered

    def by_category(self, session_id: str) -> dict[str, list[SessionFact]]:
        with sqlite3.connect(self.settings.memory_db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM session_facts "
                "WHERE session_id=? AND status='active' ORDER BY updated_at DESC",
                (session_id,),
            ).fetchall()
        grouped: dict[str, list[SessionFact]] = {}
        for row in rows:
            grouped.setdefault(row["category"], []).append(self._row_to_fact(row))
        return grouped
```

(注:`retract` 对已 retracted 的行再 UPDATE 是幂等的 —— 同样设成 `retracted`,不报错。)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/memory/test_facts_store.py -v`
Expected: PASS (9 tests: 5 from Task 3 + 4 new)

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/memory/facts.py backend/tests/memory/test_facts_store.py
git commit -m "$(cat <<'EOF'
feat(memory): SessionFactsStore retract + list_active + by_category

retract 标记 status='retracted'(软删,幂等);list_active 按 category
优先级(decision>preference>context>open_question)排序;by_category
分组,空组不出现。

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: build_facts_prompt(注入片段)

**Files:**
- Modify: `backend/src/porto_chatbot/memory/facts.py`
- Create: `backend/tests/memory/test_facts_extract.py`

**Interfaces:**
- Consumes: `SessionFactsStore.by_category(session_id)`
- Produces: `build_facts_prompt(facts: dict[str, list[SessionFact]]) -> str`(空则返回 `""`)

- [ ] **Step 1: Write the failing test**

`backend/tests/memory/test_facts_extract.py`:
```python
from __future__ import annotations

from porto_chatbot.models import SessionFact
from porto_chatbot.memory.facts import build_facts_prompt


def _fact(category, content):
    return SessionFact(
        id="x", session_id="s", category=category, content=content,
        source_msg_id="m", created_at="t", updated_at="t",
    )


def test_empty_facts_returns_empty_string():
    assert build_facts_prompt({}) == ""


def test_groups_by_category_with_headers():
    prompt = build_facts_prompt({
        "user_decision": [_fact("user_decision", "登录采用 OAuth")],
        "open_question": [_fact("open_question", "前端框架未定")],
    })
    assert "关键事实" in prompt
    assert "[决策]" in prompt
    assert "[待澄清]" in prompt
    assert "登录采用 OAuth" in prompt
    assert "前端框架未定" in prompt


def test_skips_empty_categories():
    prompt = build_facts_prompt({
        "user_decision": [_fact("user_decision", "X")],
        "open_question": [],  # 空
    })
    assert "[决策]" in prompt
    assert "[待澄清]" not in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/memory/test_facts_extract.py::test_empty_facts_returns_empty_string tests/memory/test_facts_extract.py::test_groups_by_category_with_headers tests/memory/test_facts_extract.py::test_skips_empty_categories -v`
Expected: FAIL with `ImportError: cannot import name 'build_facts_prompt'`

- [ ] **Step 3: Implement `build_facts_prompt`** (追加到 `memory/facts.py`)

```python
_CATEGORY_HEADERS: dict[str, str] = {
    "user_decision": "[决策]",
    "user_preference": "[偏好]",
    "project_context": "[背景]",
    "open_question": "[待澄清]",
}


def build_facts_prompt(grouped: dict[str, list[SessionFact]]) -> str:
    """按 category 优先级拼成 system prompt 片段。空输入返回 ""(调用方据此跳过插入)。"""
    if not grouped:
        return ""
    lines: list[str] = ["关键事实(用户已确认,优先参考):"]
    for cat in sorted(grouped, key=lambda c: _CATEGORY_PRIORITY.get(c, 99)):
        facts = grouped[cat]
        if not facts:
            continue
        header = _CATEGORY_HEADERS.get(cat, f"[{cat}]")
        lines.append(header)
        for f in facts:
            lines.append(f"- {f.content}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/memory/test_facts_extract.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/memory/facts.py backend/tests/memory/test_facts_extract.py
git commit -m "$(cat <<'EOF'
feat(memory): build_facts_prompt for system prompt injection

按 category 优先级(decision>preference>context>open_question)拼成
prompt 片段,空组跳过,全空返回空串。

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: extract_facts(LLM 提取 + JSON 解析 + 降级)

**Files:**
- Modify: `backend/src/porto_chatbot/memory/facts.py`
- Modify: `backend/tests/memory/test_facts_extract.py`(追加测试)

**Interfaces:**
- Consumes: `LLMClient.complete_structured(system, user, schema_hint) -> dict | None`;`SessionFactsStore`
- Produces: `extract_facts(*, store, llm, session_id, new_message, recent_turns, settings) -> int`(返回提取并写入的条数,失败返回 0)

- [ ] **Step 1: Write the failing tests** (追加到 `test_facts_extract.py`)

```python
from unittest.mock import MagicMock


def _make_store(tmp_path):
    from porto_chatbot.memory.store import MemoryStore
    from porto_chatbot.memory.facts import SessionFactsStore
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path)
    MemoryStore(settings)
    return SessionFactsStore(settings), settings


def test_extract_facts_writes_to_store(tmp_path):
    store, settings = _make_store(tmp_path)
    llm = MagicMock()
    llm.enabled = True
    llm.complete_structured.return_value = {
        "facts": [
            {"category": "user_decision", "content": "登录采用 OAuth", "action": "add"},
        ]
    }
    n = _call_extract(store, llm, settings, "用 OAuth 吧")
    assert n == 1
    assert len(store.list_active("s1")) == 1


def test_extract_facts_empty_result(tmp_path):
    store, settings = _make_store(tmp_path)
    llm = MagicMock()
    llm.enabled = True
    llm.complete_structured.return_value = {"facts": []}
    n = _call_extract(store, llm, settings, "你好")
    assert n == 0
    assert store.list_active("s1") == []


def test_extract_facts_llm_disabled(tmp_path):
    store, settings = _make_store(tmp_path)
    llm = MagicMock()
    llm.enabled = False
    n = _call_extract(store, llm, settings, "用 OAuth 吧")
    assert n == 0
    llm.complete_structured.assert_not_called()


def test_extract_facts_parse_failure_fail_open(tmp_path):
    store, settings = _make_store(tmp_path)
    llm = MagicMock()
    llm.enabled = True
    llm.complete_structured.return_value = None  # 解析失败
    n = _call_extract(store, llm, settings, "用 OAuth 吧")
    assert n == 0  # fail-open,不抛
    assert store.list_active("s1") == []


def test_extract_facts_retract_action(tmp_path):
    store, settings = _make_store(tmp_path)
    fid = store.upsert(session_id="s1", category="user_decision",
                       content="登录采用 OAuth", source_msg_id="m0")
    llm = MagicMock()
    llm.enabled = True
    llm.complete_structured.return_value = {
        "facts": [
            {"category": "user_decision", "content": "登录采用 OAuth", "action": "retract"},
        ]
    }
    n = _call_extract(store, llm, settings, "不用 OAuth 了")
    assert n == 1
    assert store.list_active("s1") == []  # 被 retract


def _call_extract(store, llm, settings, message):
    from porto_chatbot.memory.facts import extract_facts
    from porto_chatbot.models import MemoryRecord

    recent = [MemoryRecord(
        id="m1", session_id="s1", role="user", content="做个登录页", created_at="t",
    )]
    return extract_facts(
        store=store, llm=llm, session_id="s1",
        new_message=message, recent_turns=recent, settings=settings,
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/memory/test_facts_extract.py -v`
Expected: 5 new tests FAIL with `ImportError: cannot import name 'extract_facts'`

- [ ] **Step 3: Implement `extract_facts`** (追加到 `memory/facts.py`)

```python
_FACTS_SCHEMA_HINT = {
    "facts": [
        {
            "category": "user_decision | user_preference | project_context | open_question",
            "content": "简洁陈述,保留专有名词/变量名/数字原样",
            "action": "add | amend | retract",
        }
    ]
}

_FACTS_SYSTEM_PROMPT = """从以下最新对话中提取值得长期记住的关键事实。

只提取用户明确表达的:
- user_decision: 用户确认或否决的决定
- user_preference: 用户表达的偏好
- project_context: 项目背景、约束、领域信息
- open_question: 待澄清的问题

不提取:agent 提问、寒暄、临时试探。无事实则返回 {"facts": []}。

强制要求:content 保留所有专有名词、变量名、数字原样;每条事实原子化,不合并。

action 语义:
- add / amend: 实现上等价,都走 upsert(模糊匹配 ≥ 阈值则更新,否则新增)
- retract: 撤销事实(用户改主意,如"不用 OAuth 了"),会撤掉匹配的同 category fact"""


def extract_facts(
    *, store: SessionFactsStore, llm, session_id: str,
    new_message: str, recent_turns: list, settings: Settings,
) -> int:
    """同步提取(供 trigger_facts_extraction 在线程/to_thread 里调用)。

    LLM 不可用 / 解析失败 / 异常 → fail-open 返回 0。
    """
    if not settings.facts_enabled or not getattr(llm, "enabled", False):
        return 0
    recent_text = "\n".join(
        f"{getattr(r, 'role', 'user')}: {getattr(r, 'content', '')}" for r in recent_turns
    )
    user_prompt = f"最新用户消息:\n{new_message}\n\n最近上下文:\n{recent_text}"
    try:
        result = llm.complete_structured(
            _FACTS_SYSTEM_PROMPT, user_prompt, _FACTS_SCHEMA_HINT,
        )
    except Exception:
        store.logger.exception("facts extract llm failed session=%s", session_id)
        return 0
    if not isinstance(result, dict):
        store.logger.info("facts extract no json session=%s", session_id)
        return 0
    facts = result.get("facts") or []
    written = 0
    for item in facts:
        category = item.get("category")
        content = (item.get("content") or "").strip()
        action = item.get("action", "add")
        if category not in _CATEGORY_PRIORITY or not content:
            continue
        if action == "retract":
            _retract_by_match(store, session_id, category, content)
            written += 1
        else:  # add / amend 等价
            store.upsert(
                session_id=session_id, category=category,
                content=content, source_msg_id=None,
            )
            written += 1
    store.logger.info(
        "facts extract done session=%s extracted=%s written=%s",
        session_id, len(facts), written,
    )
    return written


def _retract_by_match(
    store: SessionFactsStore, session_id: str, category: str, content: str,
) -> None:
    """retract 时按 Jaccard 找最匹配的 active fact 撤掉;无命中则跳过。"""
    import sqlite3

    target = set(tokens(content))
    with sqlite3.connect(store.settings.memory_db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, content FROM session_facts "
            "WHERE session_id=? AND category=? AND status='active'",
            (session_id, category),
        ).fetchall()
        best_id, best_score = None, store.settings.facts_similarity_threshold
        for row in rows:
            score = _jaccard(target, set(tokens(row["content"])))
            if score >= best_score:
                best_id, best_score = row["id"], score
        if best_id is not None:
            conn.execute(
                "UPDATE session_facts SET status='retracted' WHERE id=?", (best_id,),
            )
            store.logger.info(
                "facts retract match id=%s session=%s category=%s score=%s",
                best_id, session_id, category, round(best_score, 3),
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/memory/test_facts_extract.py -v`
Expected: PASS (8 tests: 3 from Task 5 + 5 new)

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/memory/facts.py backend/tests/memory/test_facts_extract.py
git commit -m "$(cat <<'EOF'
feat(memory): extract_facts via LLMClient.complete_structured

用 complete_structured 做 JSON 提取(自带重试),LLM 不可用/解析失败/
异常均 fail-open 返回 0。add/amend 走 upsert,retract 按 Jaccard 匹配
撤掉同 category 的 active fact。

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: trigger_facts_extraction(δ-1 异步触发)

**Files:**
- Modify: `backend/src/porto_chatbot/memory/facts.py`
- Modify: `backend/tests/memory/test_facts_extract.py`(追加测试)
- Modify: `backend/src/porto_chatbot/memory/__init__.py`(导出公开 API)

**Interfaces:**
- Produces: `trigger_facts_extraction_async(...)`(供流式 `asyncio.create_task`);`trigger_facts_extraction_sync(...)`(供非流式 `threading.Thread`)

- [ ] **Step 1: Write the failing tests** (追加到 `test_facts_extract.py`)

```python
import asyncio


def test_trigger_sync_runs_in_thread(tmp_path):
    store, settings = _make_store(tmp_path)
    llm = MagicMock()
    llm.enabled = True
    llm.complete_structured.return_value = {
        "facts": [{"category": "user_decision", "content": "X", "action": "add"}]
    }
    from porto_chatbot.memory.facts import trigger_facts_extraction_sync
    from porto_chatbot.models import MemoryRecord

    recent = [MemoryRecord(id="m", session_id="s", role="user", content="x", created_at="t")]
    trigger_facts_extraction_sync(
        store=store, llm=llm, session_id="s", new_message="x",
        recent_turns=recent, settings=settings,
    )
    assert len(store.list_active("s")) == 1


def test_trigger_async_fire_and_forget(tmp_path):
    store, settings = _make_store(tmp_path)
    llm = MagicMock()
    llm.enabled = True
    llm.complete_structured.return_value = {
        "facts": [{"category": "user_decision", "content": "Y", "action": "add"}]
    }
    from porto_chatbot.memory.facts import trigger_facts_extraction_async
    from porto_chatbot.models import MemoryRecord

    recent = [MemoryRecord(id="m", session_id="s", role="user", content="y", created_at="t")]

    async def main():
        task = trigger_facts_extraction_async(
            store=store, llm=llm, session_id="s", new_message="y",
            recent_turns=recent, settings=settings,
        )
        assert task is not None
        await task  # 等异步完成
        assert len(store.list_active("s")) == 1

    asyncio.run(main())


def test_trigger_async_disabled_returns_none(tmp_path):
    store, settings = _make_store(tmp_path)
    settings.facts_enabled = False
    from porto_chatbot.memory.facts import trigger_facts_extraction_async

    task = trigger_facts_extraction_async(
        store=store, llm=MagicMock(), session_id="s", new_message="y",
        recent_turns=[], settings=settings,
    )
    assert task is None  # 直接返回,不创建任务
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/memory/test_facts_extract.py -v`
Expected: 3 new tests FAIL with `ImportError: cannot import name 'trigger_facts_extraction_sync'`

- [ ] **Step 3: Implement triggers** (追加到 `memory/facts.py`)

在文件顶部加 import:
```python
import asyncio
import threading
```

文件末尾追加:
```python
def trigger_facts_extraction_sync(
    *, store: SessionFactsStore, llm, session_id: str,
    new_message: str, recent_turns: list, settings: Settings,
) -> None:
    """非流式路径:开 daemon 线程 fire-and-forget。"""
    if not settings.facts_enabled:
        return
    t = threading.Thread(
        target=extract_facts,
        kwargs=dict(
            store=store, llm=llm, session_id=session_id,
            new_message=new_message, recent_turns=list(recent_turns),
            settings=settings,
        ),
        daemon=True,
        name=f"facts-extract-{session_id}",
    )
    t.start()
    store.logger.info("facts trigger thread started session=%s", session_id)


def trigger_facts_extraction_async(
    *, store: SessionFactsStore, llm, session_id: str,
    new_message: str, recent_turns: list, settings: Settings,
):
    """流式路径:返回 asyncio.Task 供调用方 fire-and-forget(create_task)。

    内部用 asyncio.to_thread 包装同步 extract_facts,避免阻塞事件循环。
    facts 关闭时返回 None(调用方不 create_task)。
    必须在运行中的 event loop 里调用。
    """
    if not settings.facts_enabled:
        return None
    loop = asyncio.get_running_loop()
    task = loop.create_task(
        asyncio.to_thread(
            extract_facts,
            store=store, llm=llm, session_id=session_id,
            new_message=new_message, recent_turns=list(recent_turns),
            settings=settings,
        ),
        name=f"facts-extract-{session_id}",
    )
    store.logger.info("facts trigger task created session=%s", session_id)
    return task
```

- [ ] **Step 4: Export from `memory/__init__.py`**

替换为:
```python
from .compaction import get_compacted_history, summarize_records
from .facts import (
    SessionFactsStore,
    build_facts_prompt,
    extract_facts,
    trigger_facts_extraction_async,
    trigger_facts_extraction_sync,
)
from .store import MemoryStore

__all__ = [
    "MemoryStore",
    "SessionFactsStore",
    "build_facts_prompt",
    "extract_facts",
    "get_compacted_history",
    "summarize_records",
    "trigger_facts_extraction_async",
    "trigger_facts_extraction_sync",
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/memory/ -v`
Expected: PASS (all memory tests)

- [ ] **Step 6: Commit**

```bash
git add backend/src/porto_chatbot/memory/facts.py backend/src/porto_chatbot/memory/__init__.py backend/tests/memory/test_facts_extract.py
git commit -m "$(cat <<'EOF'
feat(memory): δ-1 async/sync facts extraction triggers

trigger_facts_extraction_sync(daemon 线程,非流式路径)与
trigger_facts_extraction_async(asyncio.create_task + to_thread,
流式路径,不阻塞事件循环)。facts_enabled=False 时不创建任务。

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: a1 实体感知摘要 prompt

**Files:**
- Modify: `backend/src/porto_chatbot/memory/compaction.py:summarize_records`
- Create: `backend/tests/memory/test_compaction.py`

**Interfaces:**
- Consumes: `LLMClient.complete(system, user)`
- Produces: 强化的 `summarize_records` prompt(保留实体)

- [ ] **Step 1: Write the failing test**

`backend/tests/memory/test_compaction.py`:
```python
from __future__ import annotations

from unittest.mock import MagicMock

from porto_chatbot.memory.compaction import summarize_records
from porto_chatbot.models import MemoryRecord


def _record(role, content):
    return MemoryRecord(id="x", session_id="s", role=role, content=content, created_at="t")


def test_summarize_prompt_preserves_entities():
    """a1:摘要 system prompt 必须包含实体保留要求。"""
    llm = MagicMock()
    llm.enabled = True
    llm.complete.return_value = "摘要"
    records = [_record("user", "用 OAuth 2.0"), _record("assistant", "好的")]
    summarize_records(records, llm)
    system_arg = llm.complete.call_args.args[0]
    assert "专有名词" in system_arg
    assert "变量名" in system_arg
    assert "待澄清" in system_arg
    assert "已确认" in system_arg or "已否决" in system_arg


def test_summarize_skips_when_llm_disabled():
    llm = MagicMock()
    llm.enabled = False
    out = summarize_records([_record("user", "x")], llm)
    assert out == ""
    llm.complete.assert_not_called()


def test_summarize_empty_records():
    llm = MagicMock()
    llm.enabled = True
    assert summarize_records([], llm) == ""
    llm.complete.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/memory/test_compaction.py::test_summarize_prompt_preserves_entities -v`
Expected: FAIL(prompt 不含"专有名词"等关键词 —— 现状 prompt 太通用)

- [ ] **Step 3: Strengthen `summarize_records` prompt** (`memory/compaction.py`)

替换 `summarize_records` 函数体里的 system prompt 字符串。**原代码**:
```python
    summary = llm.complete(
        "你是会话摘要助手。把以下对话历史压缩成简洁的中文摘要，"
        "保留关键事实、用户意图、已确认的结论和未解决的问题，去掉寒暄与冗余。",
        f"对话历史:\n{transcript}",
    )
```

**改为**:
```python
    summary = llm.complete(
        "你是会话摘要助手。把以下对话历史压缩成简洁的中文摘要。\n\n"
        "强制要求:\n"
        "- 保留所有专有名词、变量名、API 名、产品名原样(不要抽象化成\"某功能\")\n"
        "- 保留所有数字、版本号、阈值\n"
        "- 保留已确认的决策(明确写\"用户确认 X\")和已否决的选项(\"用户否决 Y\")\n"
        "- 保留未解决的问题,标注\"待澄清:Z\"\n"
        "- 去掉寒暄、试探性提问、重复内容",
        f"对话历史:\n{transcript}",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/memory/test_compaction.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/memory/compaction.py backend/tests/memory/test_compaction.py
git commit -m "$(cat <<'EOF'
feat(memory): a1 entity-aware summary prompt

强化 summarize_records 的 system prompt,强制保留专有名词/变量名/数字/
版本号,显式标注已确认/已否决/待澄清,缓解旧消息摘要抽象化问题。

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: chat.py 两路注入(非流式 + 流式)+ 异步触发

**Files:**
- Modify: `backend/src/porto_chatbot/api/routes/chat.py`(非流式 `chat()` 约 `:155-172`,流式 `chat_stream()` 约 `:280-309`)
- Create: `backend/tests/api/__init__.py`(若不存在) + `backend/tests/api/test_chat_facts.py`

**Interfaces:**
- Consumes: `SessionFactsStore.list_active(session_id)` + `build_facts_prompt`;`trigger_facts_extraction_sync` / `_async`
- Produces: chat prompt 包含 facts 片段(最高优先级);每轮 user msg 后异步触发提取

- [ ] **Step 1: Write the failing integration test**

`backend/tests/api/test_chat_facts.py`:
```python
from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_chat_injects_facts_into_prompt(monkeypatch, tmp_path):
    """非流式 chat:facts 被注入 prompt_parts(检查 LLM 收到的 user 文本)。"""
    from porto_chatbot.api.routes import chat as chat_mod
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path, kb_path=tmp_path)
    captured = {}

    fake_llm = MagicMock()
    fake_llm.enabled = True
    fake_llm.complete.side_effect = lambda system, user, **kw: captured.update(
        system=system, user=user
    ) or "回答"

    # 预置一条 fact
    from porto_chatbot.memory.store import MemoryStore
    from porto_chatbot.memory.facts import SessionFactsStore

    MemoryStore(settings)
    fs = SessionFactsStore(settings)
    fs.upsert(session_id="s1", category="user_decision",
              content="登录采用 OAuth", source_msg_id="m0")

    with patch.object(chat_mod, "get_store") as gs, \
         patch.object(chat_mod, "get_memory") as gm, \
         patch.object(chat_mod, "LLMClient", return_value=fake_llm), \
         patch.object(chat_mod, "get_index_supervisor") as gi, \
         patch.object(chat_mod, "route_chat_intent") as ri:
        gs.return_value = MagicMock(search=MagicMock(return_value=[]),
                                    ensure_index=MagicMock())
        gm.return_value = MagicMock(search=MagicMock(return_value=[]),
                                    add=MagicMock(),
                                    get_messages_ordered=MagicMock(return_value=[]))
        gi.return_value.rag_available.return_value = (True, "")
        ri.return_value = MagicMock(intent="rag", reason="x")

        req = chat_mod.ChatRequest(message="用 OAuth 吧", session_id="s1")
        chat_mod.chat(req)

    assert "登录采用 OAuth" in captured["user"]
    assert "关键事实" in captured["user"]


def test_chat_stream_triggers_async_extraction(monkeypatch, tmp_path):
    """流式 chat_stream:每轮 user msg 后触发异步提取(不阻塞 SSE)。"""
    import asyncio
    from porto_chatbot.api.routes import chat as chat_mod
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path, kb_path=tmp_path)

    triggered = {}

    async def fake_trigger(**kw):
        triggered.update(kw)

    fake_llm = MagicMock()
    fake_llm.enabled = True
    fake_llm.stream = MagicMock(return_value=iter(["回答"]))

    with patch.object(chat_mod, "get_store") as gs, \
         patch.object(chat_mod, "get_memory") as gm, \
         patch.object(chat_mod, "LLMClient", return_value=fake_llm), \
         patch.object(chat_mod, "get_index_supervisor") as gi, \
         patch.object(chat_mod, "route_chat_intent") as ri, \
         patch.object(chat_mod, "trigger_facts_extraction_async", fake_trigger):
        gs.return_value = MagicMock(search=MagicMock(return_value=[]),
                                    ensure_index=MagicMock())
        gm.return_value = MagicMock(search=MagicMock(return_value=[]),
                                    add=MagicMock(),
                                    get_messages_ordered=MagicMock(return_value=[]))
        gi.return_value.rag_available.return_value = (True, "")
        ri.return_value = MagicMock(intent="rag", reason="x")

        body = {"message": "用 OAuth 吧", "session_id": "s1"}
        async for _ in chat_mod.chat_stream(body):
            pass

    assert triggered.get("session_id") == "s1"
    assert "用 OAuth 吧" in triggered.get("new_message", "")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/api/test_chat_facts.py -v`
Expected: FAIL(facts 未注入 / trigger 未被调用)

- [ ] **Step 3: Modify 非流式 `chat()`** (`api/routes/chat.py`)

在文件顶部 import 区加:
```python
from ...memory import (
    SessionFactsStore,
    build_facts_prompt,
    trigger_facts_extraction_sync,
)
```

在非流式 `chat()` 里,定位 `memory.add(session_id=req.session_id, role="user", content=req.message)` 之后(`prompt_parts = [f"用户问题:\n{req.message}"]` 之前),插入 facts 读取 + 触发:
```python
    memory.add(session_id=req.session_id, role="user", content=req.message)

    facts_store = SessionFactsStore(runtime_settings)
    facts_block = build_facts_prompt(
        facts_store.by_category(req.session_id)
    ) if runtime_settings.facts_enabled else ""
    trigger_facts_extraction_sync(
        store=facts_store, llm=llm, session_id=req.session_id,
        new_message=req.message, recent_turns=recent, settings=runtime_settings,
    )

    prompt_parts = [f"用户问题:\n{req.message}"]
    if facts_block:
        prompt_parts.append(facts_block)
    if summary:
        prompt_parts.append(f"会话历史摘要:\n{summary}")
    prompt_parts.append(
        "最近会话:\n" + "\n".join(f"{m.role}: {m.content}" for m in recent)
    )
    prompt_parts.append(f"记忆检索:\n{format_sources(memories)}")
    prompt_parts.append(f"知识库片段:\n{format_sources(sources)}")
    prompt_parts = _trim_to_budget(prompt_parts, runtime_settings.context_char_budget)
```

(替换原来不含 facts_block 的 prompt_parts 拼装段。)

- [ ] **Step 4: Modify 流式 `chat_stream()`**

流式 import 区加:
```python
from ...memory import (
    SessionFactsStore,
    build_facts_prompt,
    trigger_facts_extraction_async,
)
```

(若与非流式同文件,import 合并一次即可。)

在 `chat_stream` 的 `events()` 里,定位 `memory.add(session_id=req.session_id, role="user", content=req.message)` 之后,插入:
```python
            memory.add(session_id=req.session_id, role="user", content=req.message)

            facts_store = SessionFactsStore(runtime_settings)
            facts_block = build_facts_prompt(
                facts_store.by_category(req.session_id)
            ) if runtime_settings.facts_enabled else ""
            trigger_facts_extraction_async(
                store=facts_store, llm=llm, session_id=req.session_id,
                new_message=req.message, recent_turns=recent, settings=runtime_settings,
            )

            prompt_parts = [f"用户问题:\n{req.message}"]
            if facts_block:
                prompt_parts.append(facts_block)
            if summary:
                prompt_parts.append(f"会话历史摘要:\n{summary}")
            prompt_parts.append("最近会话:\n" + "\n".join(f"{m.role}: {m.content}" for m in recent))
            prompt_parts.append(f"记忆检索:\n{format_sources(memories)}")
            prompt_parts.append(f"知识库片段:\n{format_sources(sources)}")
            prompt_parts = _trim_to_budget(prompt_parts, runtime_settings.context_char_budget)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/api/test_chat_facts.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Run full test suite to check no regressions**

Run: `cd backend && uv run pytest -q`
Expected: PASS(所有现有测试 + 新增)

- [ ] **Step 7: Commit**

```bash
git add backend/src/porto_chatbot/api/routes/chat.py backend/tests/api/test_chat_facts.py
git commit -m "$(cat <<'EOF'
feat(memory): inject session facts into chat prompt (non-stream + stream)

非流式用 trigger_facts_extraction_sync(daemon 线程),流式用
trigger_facts_extraction_async(asyncio.create_task + to_thread,
不阻塞 SSE)。facts_block 插在"用户问题"之后,最高优先级注入。

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: GET /api/memory/{sid}/facts API(可观测)

**Files:**
- Modify: `backend/src/porto_chatbot/api/routes/memory.py`
- Create: `backend/tests/api/test_memory_facts_api.py`

**Interfaces:**
- Produces: `GET /api/memory/{session_id}/facts` → `{"session_id": str, "facts": list[SessionFact]}`

- [ ] **Step 1: Write the failing test**

`backend/tests/api/test_memory_facts_api.py`:
```python
from __future__ import annotations

from fastapi.testclient import TestClient

from porto_chatbot.main import app


def test_list_facts_empty(tmp_path, monkeypatch):
    from porto_chatbot.api.routes import memory as mem_mod
    from porto_chatbot.memory.store import MemoryStore
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path)
    MemoryStore(settings)
    monkeypatch.setattr(mem_mod, "current_settings", lambda: settings)

    client = TestClient(app)
    resp = client.get("/api/memory/s1/facts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == "s1"
    assert body["facts"] == []


def test_list_facts_returns_active(tmp_path, monkeypatch):
    from porto_chatbot.api.routes import memory as mem_mod
    from porto_chatbot.memory.facts import SessionFactsStore
    from porto_chatbot.memory.store import MemoryStore
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path)
    MemoryStore(settings)
    SessionFactsStore(settings).upsert(
        session_id="s1", category="user_decision",
        content="登录采用 OAuth", source_msg_id="m1",
    )
    monkeypatch.setattr(mem_mod, "current_settings", lambda: settings)

    client = TestClient(app)
    resp = client.get("/api/memory/s1/facts")
    body = resp.json()
    assert len(body["facts"]) == 1
    assert body["facts"][0]["content"] == "登录采用 OAuth"
    assert body["facts"][0]["category"] == "user_decision"
```

> 注:若 `porto_chatbot.main:app` 未挂 `/api/memory` 路由或不便用 TestClient,改用 `httpx.ASGITransport` 直接打 ASGI app;先确认现有 `tests/api/` 用哪种模式,与之一致。

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/api/test_memory_facts_api.py -v`
Expected: FAIL(404 或路由不存在)

- [ ] **Step 3: Add the route to `api/routes/memory.py`**

先看文件顶部已有的 import 与 `get_memory` / `current_settings` 模式,然后追加:
```python
from ...memory import SessionFactsStore
from ...models import SessionFact


@router.get("/api/memory/{session_id}/facts")
def list_session_facts(session_id: str):
    store = SessionFactsStore(current_settings())
    facts = store.list_active(session_id)
    return {"session_id": session_id, "facts": [f.model_dump() for f in facts]}
```

(具体 router 实例名 / `current_settings` 导入路径与文件现有保持一致 —— Step 3 执行时先 `Read` 该文件确认。)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/api/test_memory_facts_api.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run full test suite**

Run: `cd backend && uv run pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/src/porto_chatbot/api/routes/memory.py backend/tests/api/test_memory_facts_api.py
git commit -m "$(cat <<'EOF'
feat(memory): GET /api/memory/{sid}/facts for observability

前端可读取 session 内 active facts 列表(按 category 优先级排序),
用于展示"agent 记住了什么"。

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review 记录

**Spec 覆盖检查**:
- ✅ §2 a1 实体感知摘要 → Task 8
- ✅ §2 a2 分类结构化事实 → Task 1-7(模型/存储/提取/触发)
- ✅ §2 注入 + 可观测 → Task 9(注入)+ Task 10(API)
- ✅ §2 fail-open → Task 6(extract_facts 降级)+ Task 9(不阻塞 chat)
- ✅ §5 数据模型 → Task 1(SessionFact)+ Task 2(表)
- ✅ §6 Prompt 设计 → Task 6(提取)+ Task 8(摘要)+ Task 5(注入)
- ✅ §7 数据流 → Task 9
- ✅ §8 配置 → Task 1
- ✅ §9 错误处理 → Task 6 / Task 7 / Task 9
- ✅ §10 测试 → 每个 Task 都有 TDD

**类型一致性**:
- `SessionFact` 字段(id/session_id/category/content/status/source_msg_id/created_at/updated_at)在 Task 1 定义,Task 2 表 schema、Task 3 `_row_to_fact`、Task 10 `model_dump` 全部对齐
- `SessionFactsStore.upsert` 签名在 Task 3 定义,Task 6 `extract_facts` 调用一致
- `build_facts_prompt(grouped: dict[str, list[SessionFact]])` 在 Task 5 定义,Task 9 用 `by_category` 返回值调用一致
- `trigger_facts_extraction_sync` / `_async` 在 Task 7 定义,Task 9 两路调用一致
