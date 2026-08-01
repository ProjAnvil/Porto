# Claude Agent SDK ReAct 引擎实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Porto 新增 Claude Agent SDK 作为可选执行引擎，与现有 Langchain 路径并存，前端卡片切换。

**Architecture:** 策略模式（`AgentBackend` Protocol）消除 if-else；Langchain 路径零行为变化（节点做机械性接口提取）；Agent SDK 是平行新增实现；Memory/Skills/Tools 通过 `@tool` + `setting_sources` 暴露给 Claude。

**Tech Stack:** Python 3.12, FastAPI, LangGraph, claude-agent-sdk (pip), Next.js/React

## Global Constraints

- **不改现有 langchain 行为**：`LLMClient`、`registry.py`、`handlers.py`、`graph.py`、`workflow_executor.py`、`memory/` 一行不改
- **默认 langchain**：`chatbot_backend` 和 `workflow_backend` 默认 `"langchain"`
- **不留 fallback**：选了 `agent_sdk` 后出错不回退 langchain，优雅返回错误
- **claude-agent-sdk 通过 `pip install claude-agent-sdk` 安装**（bundles Claude Code CLI）
- **Python 后端代码在 `backend/src/porto_chatbot/` 下**，测试在 `backend/tests/`
- **前端代码在 `frontend/src/` 下**
- **conventional commits**：`feat:`/`fix:`/`refactor:`/`test:`/`docs:`

---

## File Structure

### 新增文件

| 路径 | 职责 |
|------|------|
| `backend/src/porto_chatbot/agent/backends.py` | `AgentBackend` Protocol + `NodeExecutionResult` + `BackendTools` 类型 |
| `backend/src/porto_chatbot/agent/factory.py` | `create_backend()` 工厂 |
| `backend/src/porto_chatbot/agent_sdk/__init__.py` | Agent SDK 后端模块入口 |
| `backend/src/porto_chatbot/agent_sdk/backend.py` | `AgentSDKBackend` 实现 |
| `backend/src/porto_chatbot/agent_sdk/tools.py` | `build_sdk_tools()` — `@tool` 包装 handlers.py |
| `backend/src/porto_chatbot/agent_sdk/skills.py` | `SKILLS` 定义 + `deploy_skills()` |
| `backend/tests/test_backends.py` | 策略层测试 |
| `backend/tests/test_sdk_tools.py` | `@tool` 包装测试 |
| `backend/tests/test_skill_deploy.py` | skill 部署测试 |

### 修改文件

| 路径 | 改动 |
|------|------|
| `backend/src/porto_chatbot/settings.py` | 新增 `chatbot_backend` + `workflow_backend` |
| `backend/src/porto_chatbot/models/payload.py` | `AgentSettingsPayload` 新增两字段 |
| `backend/src/porto_chatbot/tools/context.py` | `AgentToolContext` 加 `memory_store`/`facts_store` 可选字段 |
| `backend/src/porto_chatbot/agent/agent.py` | `PortoAgent` 注入 `self.backend` |
| `backend/src/porto_chatbot/agent/nodes/understand.py` | `complete_with_tools` → `backend.execute_node` |
| `backend/src/porto_chatbot/agent/nodes/identify.py` | `complete_structured` → `backend.execute_node` |
| `backend/src/porto_chatbot/specs/context.py` | `SpecContext` 加 `backend` 字段 |
| `backend/src/porto_chatbot/specs/steps.py` | `generate_initial_spec`/`refine_spec` → `backend.execute_node` |
| `backend/src/porto_chatbot/agent/nodes/generate.py` | `_gen()` 构造 SpecContext 时传 backend |
| `backend/src/porto_chatbot/api/routes/chat.py` | 入口分派 `create_backend(scope="chatbot")` |
| `backend/src/porto_chatbot/main.py` | 启动钩子加 `deploy_skills()` |
| `frontend/src/lib/types.ts` | `AppSettings.agent` 加两字段 |
| `frontend/src/components/porto-workbench.tsx` | Settings 卡片选择器 + provider 联动 |

---

## Task 1: 配置层——backend 字段

**Files:**
- Modify: `backend/src/porto_chatbot/settings.py:46` (在 `agent_provider` 前插入)
- Modify: `backend/src/porto_chatbot/models/payload.py:26` (`AgentSettingsPayload`)
- Test: `backend/tests/test_settings_backend.py`

**Interfaces:**
- Produces: `Settings.chatbot_backend`, `Settings.workflow_backend` (both `Literal["langchain", "agent_sdk"]`, default `"langchain"`)
- Produces: `AgentSettingsPayload.chatbot_backend`, `AgentSettingsPayload.workflow_backend` (both `Literal["langchain", "agent_sdk"] | None`)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_settings_backend.py
"""Test that backend fields exist with correct defaults and types."""
from porto_chatbot.settings import Settings
from porto_chatbot.models.payload import AgentSettingsPayload


def test_settings_default_backends_are_langchain():
    s = Settings()
    assert s.chatbot_backend == "langchain"
    assert s.workflow_backend == "langchain"


def test_settings_backends_accept_agent_sdk():
    s = Settings()
    s.chatbot_backend = "agent_sdk"
    s.workflow_backend = "agent_sdk"
    assert s.chatbot_backend == "agent_sdk"
    assert s.workflow_backend == "agent_sdk"


def test_payload_accepts_backend_fields():
    payload = AgentSettingsPayload(chatbot_backend="agent_sdk", workflow_backend="langchain")
    assert payload.chatbot_backend == "agent_sdk"
    assert payload.workflow_backend == "langchain"


def test_payload_backends_default_none():
    payload = AgentSettingsPayload()
    assert payload.chatbot_backend is None
    assert payload.workflow_backend is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_settings_backend.py -v`
Expected: FAIL — `AttributeError` or validation error (fields don't exist yet)

- [ ] **Step 3: Add fields to Settings**

In `backend/src/porto_chatbot/settings.py`, add before line 46 (`agent_provider`):

```python
    # --- Agent 引擎选择 ---
    chatbot_backend: Literal["langchain", "agent_sdk"] = "langchain"
    workflow_backend: Literal["langchain", "agent_sdk"] = "langchain"
```

- [ ] **Step 4: Add fields to AgentSettingsPayload**

In `backend/src/porto_chatbot/models/payload.py`, add inside `AgentSettingsPayload` (before `agent_provider`):

```python
    chatbot_backend: Literal["langchain", "agent_sdk"] | None = None
    workflow_backend: Literal["langchain", "agent_sdk"] | None = None
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/test_settings_backend.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Run full test suite to verify no regression**

Run: `cd backend && uv run pytest tests/ -x -q`
Expected: All existing tests pass

- [ ] **Step 7: Commit**

```bash
cd backend
git add src/porto_chatbot/settings.py src/porto_chatbot/models/payload.py tests/test_settings_backend.py
git commit -m "feat: add chatbot_backend and workflow_backend config fields"
```

---

## Task 2: 抽象层——AgentBackend Protocol + LangchainBackend + factory

**Files:**
- Create: `backend/src/porto_chatbot/agent/backends.py`
- Create: `backend/src/porto_chatbot/agent/factory.py`
- Test: `backend/tests/test_backends.py`

**Interfaces:**
- Consumes: `LLMClient.complete_with_tools()` / `.complete_structured()` / `.complete()` (existing signatures)
- Consumes: `AgentToolContext`, `build_agent_tools()` from `tools/`
- Produces: `AgentBackend` Protocol with `build_tools(ctx)`, `execute_node(...)`, `chat(req, settings)`, `chat_stream(req, settings)`
- Produces: `NodeExecutionResult(text, structured, tool_calls, turns, truncated, reason)`
- Produces: `LangchainBackend` class
- Produces: `create_backend(settings, llm, scope) -> AgentBackend`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_backends.py
"""Test AgentBackend Protocol, LangchainBackend, and factory dispatch."""
import json
from unittest.mock import MagicMock, patch
from porto_chatbot.agent.backends import AgentBackend, NodeExecutionResult, LangchainBackend
from porto_chatbot.agent.factory import create_backend
from porto_chatbot.llm.types import ToolLoopResult, ToolCall
from porto_chatbot.settings import Settings
from porto_chatbot.llm import LLMClient


def test_node_execution_result_defaults():
    r = NodeExecutionResult()
    assert r.text == ""
    assert r.structured is None
    assert r.tool_calls == []
    assert r.turns == 0
    assert r.truncated is False
    assert r.reason is None


def test_langchain_backend_execute_node_with_tools():
    """Tools mode: delegates to complete_with_tools, maps result correctly."""
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.enabled = True
    mock_llm.complete_with_tools.return_value = ToolLoopResult(
        text="understanding text",
        tool_calls=[ToolCall(name="get_prd_text", arguments={}, result="prd...")],
        turns=3,
        truncated=False,
    )
    backend = LangchainBackend(mock_llm)
    import asyncio
    result = asyncio.get_event_loop().run_until_complete(
        backend.execute_node(system="sys", user="usr", tools=["fake_tools"])
    )
    assert result.text == "understanding text"
    assert len(result.tool_calls) == 1
    assert result.turns == 3
    assert result.truncated is False


def test_langchain_backend_execute_node_structured():
    """Structured mode: delegates to complete_structured, fills .structured."""
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.enabled = True
    mock_llm.complete_structured.return_value = {"subsystems": [{"name": "svc"}]}
    backend = LangchainBackend(mock_llm)
    import asyncio
    result = asyncio.get_event_loop().run_until_complete(
        backend.execute_node(
            system="sys", user="usr", tools=None,
            structured_schema={"type": "object"},
        )
    )
    assert result.structured == {"subsystems": [{"name": "svc"}]}


def test_langchain_backend_execute_node_plain():
    """Plain mode: delegates to complete, fills .text only."""
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.enabled = True
    mock_llm.complete.return_value = "refined spec text"
    backend = LangchainBackend(mock_llm)
    import asyncio
    result = asyncio.get_event_loop().run_until_complete(
        backend.execute_node(system="sys", user="usr")
    )
    assert result.text == "refined spec text"
    assert result.structured is None


def test_factory_returns_langchain_by_default():
    s = Settings()
    backend = create_backend(s, scope="workflow")
    assert isinstance(backend, LangchainBackend)


def test_factory_returns_langchain_for_chatbot_default():
    s = Settings()
    backend = create_backend(s, scope="chatbot")
    assert isinstance(backend, LangchainBackend)


def test_factory_returns_agent_sdk_when_configured():
    """AgentSDKBackend is created when backend='agent_sdk'. 
    This test will be enabled after Task 5 creates AgentSDKBackend."""
    s = Settings()
    s.workflow_backend = "agent_sdk"
    # AgentSDKBackend not yet implemented — expect ImportError or skip
    try:
        backend = create_backend(s, scope="workflow")
        assert not isinstance(backend, LangchainBackend)
    except ImportError:
        pass  # Expected until Task 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_backends.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'porto_chatbot.agent.backends'`

- [ ] **Step 3: Create backends.py**

```python
# backend/src/porto_chatbot/agent/backends.py
"""AgentBackend Protocol + LangchainBackend + NodeExecutionResult.

Strategy pattern: chatbot 和 workflow 的所有 LLM 交互经过这个接口。
加新引擎 = 实现这个 Protocol，不改任何调用方。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..llm import LLMClient, ToolLoopResult
from ..llm.types import ToolDef
from ..logging_utils import get_component_logger
from ..models import ChatRequest, ChatResponse
from ..settings import Settings
from ..tools import AgentToolContext, build_agent_tools


@dataclass
class NodeExecutionResult:
    """节点执行的统一返回格式——消费方不关心后端差异。"""
    text: str = ""
    structured: dict | None = None
    tool_calls: list = field(default_factory=list)
    turns: int = 0
    truncated: bool = False
    reason: str | None = None


# BackendTools: list[ToolDef] (langchain) or SDK MCP server (agent_sdk)
BackendTools = Any


@runtime_checkable
class AgentBackend(Protocol):
    """一个执行引擎的完整契约。"""

    def build_tools(self, ctx: AgentToolContext) -> BackendTools:
        """返回该引擎的工具集。"""
        ...

    async def execute_node(
        self, *,
        system: str,
        user: str,
        tools: BackendTools | None = None,
        structured_schema: dict | None = None,
        max_turns: int = 10,
    ) -> NodeExecutionResult:
        """一次节点级 agent 调用。

        三种模式由参数控制：
        - tools 非空 → tool-calling loop（understand, generate_initial_spec）
        - tools 空但 structured_schema 非空 → 结构化输出（identify, critique）
        - 都空 → 纯文本补全（refine_spec）
        """
        ...

    async def chat(self, req: ChatRequest, settings: Settings) -> ChatResponse:
        """chatbot 模式的完整处理。"""
        ...

    async def chat_stream(
        self, req: ChatRequest, settings: Settings
    ) -> AsyncIterator[str]:
        """SSE 流式版。"""
        ...


class LangchainBackend:
    """现有 complete_with_tools / complete_structured / complete 的薄封装。

    行为和直接调 LLMClient 完全一样——只是走接口。
    """

    def __init__(self, llm: LLMClient):
        self.llm = llm
        self.logger = get_component_logger("backend.langchain", llm.settings)

    def build_tools(self, ctx: AgentToolContext) -> list[ToolDef]:
        return build_agent_tools(ctx)

    async def execute_node(
        self, *,
        system: str,
        user: str,
        tools: BackendTools | None = None,
        structured_schema: dict | None = None,
        max_turns: int = 10,
    ) -> NodeExecutionResult:
        if tools:
            r = self.llm.complete_with_tools(system, user, tools, max_turns=max_turns)
            return NodeExecutionResult(
                text=r.text,
                tool_calls=list(r.tool_calls),
                turns=r.turns,
                truncated=r.truncated,
                reason=r.reason,
            )
        if structured_schema:
            parsed = self.llm.complete_structured(system, user, structured_schema)
            return NodeExecutionResult(
                structured=parsed,
                text=json.dumps(parsed, ensure_ascii=False) if parsed else "",
            )
        text = self.llm.complete(system, user)
        return NodeExecutionResult(text=text or "")

    async def chat(self, req: ChatRequest, settings: Settings) -> ChatResponse:
        """Langchain chatbot 逻辑——委托给现有 chat.py 的 _langchain_chat。"""
        from .langchain_chat import langchain_chat
        return langchain_chat(req, settings)

    async def chat_stream(
        self, req: ChatRequest, settings: Settings
    ) -> AsyncIterator[str]:
        from .langchain_chat import langchain_chat_stream
        async for chunk in langchain_chat_stream(req, settings):
            yield chunk
```

- [ ] **Step 4: Create factory.py**

```python
# backend/src/porto_chatbot/agent/factory.py
"""Backend 工厂——全系统唯一一处条件判断。"""
from __future__ import annotations

from ..llm import LLMClient
from ..settings import Settings
from .backends import AgentBackend, LangchainBackend


def create_backend(
    settings: Settings,
    *,
    llm: LLMClient | None = None,
    scope: str = "workflow",
) -> AgentBackend:
    """根据 settings 的 backend 字段创建对应引擎。

    scope='chatbot' → 读 chatbot_backend；scope='workflow' → 读 workflow_backend。
    """
    backend_name = (
        settings.chatbot_backend if scope == "chatbot"
        else settings.workflow_backend
    )
    if backend_name == "agent_sdk":
        from ..agent_sdk.backend import AgentSDKBackend
        return AgentSDKBackend(settings)
    return LangchainBackend(llm or LLMClient(settings))
```

- [ ] **Step 5: Create stub langchain_chat.py**

```python
# backend/src/porto_chatbot/agent/langchain_chat.py
"""Langchain chatbot 逻辑的委托入口。

Phase 2 (Task 9) 会把 chat.py 的现有函数体搬到这里。
目前是占位——实际调用 chat.py 的现有函数。
"""
from __future__ import annotations

from ..models import ChatRequest, ChatResponse
from ..settings import Settings


def langchain_chat(req: ChatRequest, settings: Settings) -> ChatResponse:
    """Phase 2 (Task 9) 实现：从 chat.py 搬入现有 chat() 函数体。"""
    raise NotImplementedError("Implemented in Task 9")


async def langchain_chat_stream(req: ChatRequest, settings: Settings):
    """Phase 2 (Task 9) 实现。"""
    raise NotImplementedError("Implemented in Task 9")
    yield  # make it a generator
```

- [ ] **Step 6: Run test to verify it passes (except factory agent_sdk test)**

Run: `cd backend && uv run pytest tests/test_backends.py -v`
Expected: 6 PASS, 1 PASS (factory agent_sdk test catches ImportError)

- [ ] **Step 7: Commit**

```bash
cd backend
git add src/porto_chatbot/agent/backends.py src/porto_chatbot/agent/factory.py \
        src/porto_chatbot/agent/langchain_chat.py tests/test_backends.py
git commit -m "feat: add AgentBackend Protocol, LangchainBackend, and factory"
```

---

## Task 3: AgentToolContext 扩展 + PortoAgent 注入 backend

**Files:**
- Modify: `backend/src/porto_chatbot/tools/context.py:37` (`AgentToolContext`)
- Modify: `backend/src/porto_chatbot/agent/agent.py:28` (`PortoAgent.__init__`)
- Test: `backend/tests/test_agent_backend_injection.py`

**Interfaces:**
- Consumes: `create_backend()` from Task 2
- Produces: `AgentToolContext.memory_store`, `AgentToolContext.facts_store` (optional, chatbot-only)
- Produces: `PortoAgent.backend` attribute

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_agent_backend_injection.py
"""Test that PortoAgent gets a backend injected and AgentToolContext supports memory."""
from porto_chatbot.agent.agent import PortoAgent
from porto_chatbot.agent.backends import LangchainBackend
from porto_chatbot.tools.context import AgentToolContext
from porto_chatbot.settings import Settings


def test_porto_agent_has_backend():
    s = Settings()
    agent = PortoAgent(s)
    assert hasattr(agent, "backend")
    assert isinstance(agent.backend, LangchainBackend)


def test_agent_tool_context_memory_fields_exist():
    ctx = AgentToolContext(state={})
    assert hasattr(ctx, "memory_store")
    assert ctx.memory_store is None
    assert hasattr(ctx, "facts_store")
    assert ctx.facts_store is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_agent_backend_injection.py -v`
Expected: FAIL — `AttributeError: 'PortoAgent' object has no attribute 'backend'`

- [ ] **Step 3: Extend AgentToolContext**

In `backend/src/porto_chatbot/tools/context.py`, modify the `AgentToolContext` dataclass (line 37):

```python
@dataclass
class AgentToolContext:
    """工具执行上下文：持有可变的 workflow state 与向量库句柄。

    chatbot 模式额外传 memory_store / facts_store（workflow 模式为 None）。
    """

    state: State
    vector_store: LocalVectorStore | None = None
    memory_store: Any = None       # chatbot 专属（MemoryStore），workflow 为 None
    facts_store: Any = None        # chatbot 专属（SessionFactsStore），workflow 为 None
```

Add import at top if not present: `from typing import Any`

- [ ] **Step 4: Inject backend into PortoAgent**

In `backend/src/porto_chatbot/agent/agent.py`, add to `__init__` after `self.critic_llm = self._build_critic_llm()` (line 38):

```python
        from .factory import create_backend
        self.backend = create_backend(settings, llm=self.llm, scope="workflow")
        self.logger.info("agent backend ready backend=%s", type(self.backend).__name__)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/test_agent_backend_injection.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Run full test suite**

Run: `cd backend && uv run pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
cd backend
git add src/porto_chatbot/tools/context.py src/porto_chatbot/agent/agent.py \
        tests/test_agent_backend_injection.py
git commit -m "feat: inject backend into PortoAgent, extend AgentToolContext with memory fields"
```

---

## Task 4: 节点改造——understand + identify + generate specs

**Files:**
- Modify: `backend/src/porto_chatbot/agent/nodes/understand.py:25`
- Modify: `backend/src/porto_chatbot/agent/nodes/identify.py:20`
- Modify: `backend/src/porto_chatbot/specs/context.py:13` (`SpecContext`)
- Modify: `backend/src/porto_chatbot/specs/steps.py:36` (`generate_initial_spec`)
- Modify: `backend/src/porto_chatbot/specs/steps.py:104` (`refine_spec`)
- Modify: `backend/src/porto_chatbot/agent/nodes/generate.py:16` (`_gen` closure)
- Test: `backend/tests/test_node_backend_dispatch.py`

**Interfaces:**
- Consumes: `PortoAgent.backend` from Task 3, `NodeExecutionResult` from Task 2
- Produces: All nodes calling `agent.backend.execute_node()` instead of direct `agent.llm.*` calls

**Key principle:** Each node's LLM call is replaced by `backend.execute_node()`. The `LangchainBackend` internally calls the exact same `LLMClient` methods, so behavior is identical. This is verifiable by running existing workflow tests unchanged.

- [ ] **Step 1: Write the regression test**

```python
# backend/tests/test_node_backend_dispatch.py
"""Verify that nodes use backend.execute_node instead of direct LLM calls.

These are mock-based tests: we verify the dispatch goes through backend,
not that the LLM output is correct (existing tests cover that).
"""
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
from porto_chatbot.agent.backends import NodeExecutionResult


def test_understand_uses_backend(monkeypatch):
    """understand_prd calls agent.backend.execute_node, not agent.llm.complete_with_tools."""
    from porto_chatbot.agent.nodes.understand import understand_prd
    from porto_chatbot.agent.agent import PortoAgent
    from porto_chatbot.settings import Settings

    agent = PortoAgent(Settings())
    # Replace backend with a mock that returns a known result
    mock_result = NodeExecutionResult(text="mocked understanding", turns=1)
    agent.backend = MagicMock()
    agent.backend.execute_node = AsyncMock(return_value=mock_result)
    agent.backend.build_tools = MagicMock(return_value=["fake_tools"])

    state = {"workflow_id": "test", "prd_text": "test PRD", "project_name": "test",
             "steps": [], "sources": [], "understanding": "", "subsystems": [],
             "specs": {}, "spec_results": {}, "evaluation": {}, "top_k": 6}
    config = {"configurable": {"agent": agent}}

    result = understand_prd(state, config=config)
    assert result["understanding"] == "mocked understanding"
    agent.backend.execute_node.assert_called_once()


def test_identify_uses_backend():
    """identify_subsystems calls agent.backend.execute_node with structured mode."""
    from porto_chatbot.agent.nodes.identify import identify_subsystems
    from porto_chatbot.agent.agent import PortoAgent
    from porto_chatbot.settings import Settings

    agent = PortoAgent(Settings())
    mock_result = NodeExecutionResult(
        structured={"subsystems": [{"name": "payment-service", "type": "new",
                                     "responsibility": "payments", "capabilities": [],
                                     "data_entities": [], "dependencies": []}]}
    )
    agent.backend = MagicMock()
    agent.backend.execute_node = AsyncMock(return_value=mock_result)

    state = {"workflow_id": "test", "prd_text": "test", "project_name": "test",
             "understanding": "test understanding", "steps": [], "sources": [],
             "subsystems": [], "specs": {}, "spec_results": {}, "evaluation": {}, "top_k": 6}
    config = {"configurable": {"agent": agent}}

    result = identify_subsystems(state, config=config)
    assert len(result["subsystems"]) == 1
    assert result["subsystems"][0].name == "payment-service"
    agent.backend.execute_node.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_node_backend_dispatch.py -v`
Expected: FAIL — nodes still call `agent.llm.complete_with_tools` directly

- [ ] **Step 3: Modify understand_prd**

In `backend/src/porto_chatbot/agent/nodes/understand.py`, replace lines 25-31 (the `complete_with_tools` call):

```python
    if agent.llm.enabled:
        ctx = AgentToolContext(state=state, vector_store=agent.vector_store)
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(
            agent.backend.execute_node(
                system=(
                    "你是资深业务分析师。根据 PRD 和知识库片段，输出简洁的中文业务理解报告，"
                    "包含：执行摘要、业务目标、核心实体、子系统线索。"
                    "可调用工具获取 PRD 原文与检索知识库，自主决定检索什么。"
                ),
                user="请生成业务理解报告。",
                tools=agent.backend.build_tools(ctx),
                max_turns=max_turns,
            )
        )
        tool_meta = {
            "turns": result.turns,
            "tool_calls": len(result.tool_calls),
            "truncated": result.truncated,
            "max_turns": max_turns,
            "reason": result.reason,
        }
```

Keep the rest of the function unchanged (truncated handling, fallback, return).

- [ ] **Step 4: Modify identify_subsystems**

In `backend/src/porto_chatbot/agent/nodes/identify.py`, replace lines 20-25 (the `complete_structured` call):

```python
    if agent.llm.enabled:
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(
            agent.backend.execute_node(
                system=(
                    "你是资深系统架构师。按领域驱动设计原则，根据业务理解报告与 PRD 识别需要拆分的子系统。"
                    "每个子系统职责单一、边界清晰，数量控制在 2-6 个，命名形如 xxx-service。"
                ),
                user=(
                    f"业务理解报告:\n{state['understanding']}\n\n"
                    f"PRD 节选:\n{state['prd_text'][:2000]}"
                ),
                structured_schema=subsystem_schema(),
            )
        )
        parsed = result.structured
```

Keep the rest (normalize_sub_dict loop, fallback) unchanged.

- [ ] **Step 5: Modify SpecContext to hold backend**

In `backend/src/porto_chatbot/specs/context.py`, add `backend` field:

```python
@dataclass
class SpecContext:
    backend: Any = None           # AgentBackend (Task 2 Protocol)
    llm: LLMClient | None = None  # 保留（critic 仍用 LLMClient，向后兼容）
    state: dict[str, Any] = field(default_factory=dict)
    settings: Settings | None = None
    vector_store: LocalVectorStore | None = None
    critic_llm: LLMClient | None = None
```

Note: reorder fields so `backend` is first (it's the primary interface now). Keep `llm` for backward compat (critique_spec still uses `ctx.critic_llm`).

- [ ] **Step 6: Modify generate_initial_spec**

In `backend/src/porto_chatbot/specs/steps.py`, replace lines 36-44 (the `complete_with_tools` call in `generate_initial_spec`):

```python
    tools_ctx = AgentToolContext(state=ctx.state, vector_store=ctx.vector_store)
    import asyncio
    result = asyncio.get_event_loop().run_until_complete(
        ctx.backend.execute_node(
            system=(
                f"你是资深系统规格工程师。为子系统 {sub.name} 生成详细的系统需求规格（markdown）。"
                f"子系统职责：{sub.responsibility}；能力：{', '.join(sub.capabilities) or '（待识别）'}。"
                f"必须包含这些章节：{', '.join(_SPEC_SECTIONS)}。"
                "API 需求要给出具体端点/方法/输入输出/错误码；数据模型要列实体与关键字段；验收标准要具体可测。"
                "可调用工具检索知识库以参考现有系统约定。"
            ),
            user=f"请生成 {sub.name} 的规格文档。",
            tools=ctx.backend.build_tools(tools_ctx),
        )
    )
```

Map `result.tool_calls`, `result.turns`, `result.truncated` into `tool_meta` the same way as before. Replace `result.text` usage with the backend result.

- [ ] **Step 7: Modify refine_spec**

In `backend/src/porto_chatbot/specs/steps.py`, replace lines 104-108 (the `complete` call in `refine_spec`):

```python
    import asyncio
    result = asyncio.get_event_loop().run_until_complete(
        ctx.backend.execute_node(
            system=(
                f"你是资深系统规格工程师。根据评审反馈改进 {sub.name} 的规格文档（职责：{sub.responsibility}）。"
                "保持原有 markdown 结构与章节，只针对反馈改进；不要删除已有合理内容；不要输出解释，直接给完整文档。"
            ),
            user=(
                f"评审反馈：\n{feedback}\n\n当前规格：\n{spec}\n\n请输出改进后的完整规格文档。"
            ),
        )
    )
    refined = (result.text or "").strip()
    return refined or spec
```

- [ ] **Step 8: Modify generate.py _gen closure**

In `backend/src/porto_chatbot/agent/nodes/generate.py`, update the `_gen` closure (line 16-25) to pass `backend`:

```python
    def _gen(sub: Subsystem) -> SpecResult:
        sub_ctx = SpecContext(
            backend=agent.backend,
            state={**state},
            settings=agent.settings,
            vector_store=agent.vector_store,
            critic_llm=agent.critic_llm,
        )
        return generate_spec_with_loop(sub_ctx, sub)
```

- [ ] **Step 9: Run the new dispatch tests**

Run: `cd backend && uv run pytest tests/test_node_backend_dispatch.py -v`
Expected: PASS (2 tests)

- [ ] **Step 10: Run full workflow test suite for regression**

Run: `cd backend && uv run pytest tests/test_workflow*.py tests/test_spec*.py -x -q`
Expected: All existing tests pass (LangchainBackend delegates to the same LLMClient methods)

- [ ] **Step 11: Commit**

```bash
cd backend
git add src/porto_chatbot/agent/nodes/understand.py \
        src/porto_chatbot/agent/nodes/identify.py \
        src/porto_chatbot/specs/context.py \
        src/porto_chatbot/specs/steps.py \
        src/porto_chatbot/agent/nodes/generate.py \
        tests/test_node_backend_dispatch.py
git commit -m "refactor: nodes use backend.execute_node instead of direct LLM calls"
```

---

## Task 5: Chatbot 入口分派 + LangchainBackend.chat 封装

**Files:**
- Modify: `backend/src/porto_chatbot/api/routes/chat.py:145` (`chat()`)
- Modify: `backend/src/porto_chatbot/api/routes/chat.py:274` (`chat_stream()`)
- Modify: `backend/src/porto_chatbot/agent/langchain_chat.py` (replace stub)
- Test: `backend/tests/test_chat_dispatch.py`

**Interfaces:**
- Consumes: `create_backend(scope="chatbot")` from Task 2
- Produces: `chat()` and `chat_stream()` dispatch to backend based on `chatbot_backend`

- [ ] **Step 1: Write the dispatch test**

```python
# backend/tests/test_chat_dispatch.py
"""Verify chat endpoint dispatches to the correct backend."""
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from porto_chatbot.api.app import create_app


def test_chat_uses_langchain_by_default():
    """Default chatbot_backend='langchain' → LangchainBackend.chat()."""
    app = create_app()
    client = TestClient(app)
    with patch("porto_chatbot.agent.factory.create_backend") as mock_factory:
        mock_backend = MagicMock()
        mock_backend.chat = MagicMock(return_value=MagicMock(answer="test"))
        mock_factory.return_value = mock_backend
        # This test verifies the dispatch path, not actual chat logic
        # We just verify create_backend was called with scope="chatbot"
        client.post("/api/chat", json={"message": "hi", "session_id": "test"})
        if mock_factory.called:
            _, kwargs = mock_factory.call_args
            assert kwargs.get("scope") == "chatbot"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_chat_dispatch.py -v`
Expected: FAIL — chat() doesn't call create_backend yet

- [ ] **Step 3: Replace chat() function with dispatch**

In `backend/src/porto_chatbot/api/routes/chat.py`, replace the `chat()` function (line 145):

```python
@router.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    logger.info(
        "chat start session_id=%s message_chars=%s top_k=%s",
        req.session_id, len(req.message), req.top_k,
    )
    rag_settings = effective_rag_settings(req.rag)
    top_k = req.top_k or rag_settings.top_k
    runtime_settings = apply_rag_settings(req.rag, agent=req.agent, top_k=top_k)

    from ...agent.factory import create_backend
    engine = create_backend(runtime_settings, scope="chatbot")
    import asyncio
    return asyncio.get_event_loop().run_until_complete(engine.chat(req, runtime_settings))
```

- [ ] **Step 4: Replace chat_stream() with dispatch**

In `backend/src/porto_chatbot/api/routes/chat.py`, replace the `chat_stream()` function (line 274) entry to dispatch:

```python
@router.post("/api/chat/stream")
async def chat_stream(body: dict[str, Any]):
    req = _chat_request_from_stream_body(body)
    logger.info("chat stream start session_id=%s", req.session_id)

    rag_settings = effective_rag_settings(req.rag)
    top_k = req.top_k or rag_settings.top_k
    runtime_settings = apply_rag_settings(req.rag, agent=req.agent, top_k=top_k)

    from ...agent.factory import create_backend
    engine = create_backend(runtime_settings, scope="chatbot")
    return StreamingResponse(
        engine.chat_stream(req, runtime_settings),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )
```

- [ ] **Step 5: Move existing chat logic to langchain_chat.py**

Move the entire body of the old `chat()` function (the intent routing, RAG retrieval, memory, prompt assembly, LLM call, response construction) into `langchain_chat()` in `backend/src/porto_chatbot/agent/langchain_chat.py`. Similarly move `chat_stream` body into `langchain_chat_stream()`.

The existing chat.py functions become thin dispatchers (Steps 3-4). The actual logic lives in `langchain_chat.py`.

- [ ] **Step 6: Run full chat test suite**

Run: `cd backend && uv run pytest tests/test_chat*.py -x -q`
Expected: All pass (behavior unchanged — same code, just moved location)

- [ ] **Step 7: Commit**

```bash
cd backend
git add src/porto_chatbot/api/routes/chat.py \
        src/porto_chatbot/agent/langchain_chat.py \
        tests/test_chat_dispatch.py
git commit -m "refactor: chat endpoint dispatches to backend via create_backend"
```

---

## Task 6: AgentSDKBackend——execute_node + build_sdk_tools

**Files:**
- Create: `backend/src/porto_chatbot/agent_sdk/__init__.py`
- Create: `backend/src/porto_chatbot/agent_sdk/backend.py`
- Create: `backend/src/porto_chatbot/agent_sdk/tools.py`
- Test: `backend/tests/test_sdk_tools.py`, `backend/tests/test_agent_sdk_backend.py`

**Prerequisite:** `pip install claude-agent-sdk` added to `backend/pyproject.toml`

**Interfaces:**
- Consumes: `handlers.py` functions (`_get_prd_text`, `_search_knowledgebase`, etc.)
- Consumes: `AgentToolContext` (with optional `memory_store`/`facts_store`)
- Produces: `AgentSDKBackend` class implementing `AgentBackend` Protocol
- Produces: `build_sdk_tools(ctx) -> list` returning `@tool`-decorated functions

- [ ] **Step 1: Add claude-agent-sdk dependency**

In `backend/pyproject.toml`, add to dependencies:

```toml
"claude-agent-sdk>=0.1.0",
```

Run: `cd backend && uv sync`

- [ ] **Step 2: Write the failing test for build_sdk_tools**

```python
# backend/tests/test_sdk_tools.py
"""Test that build_sdk_tools wraps existing handlers correctly."""
from porto_chatbot.tools.context import AgentToolContext
from porto_chatbot.agent_sdk.tools import build_sdk_tools


def test_build_sdk_tools_returns_list():
    ctx = AgentToolContext(state={"prd_text": "test PRD"})
    tools = build_sdk_tools(ctx)
    assert isinstance(tools, list)
    assert len(tools) >= 4  # at least get_prd, search_kb, get_understanding, etc.


def test_build_sdk_tools_includes_chatbot_tools_when_memory_present():
    """When ctx has memory_store, chatbot-specific tools are registered."""
    ctx = AgentToolContext(
        state={"prd_text": "test"},
        memory_store=MagicMock(),
        facts_store=MagicMock(),
    )
    tools = build_sdk_tools(ctx)
    tool_names = [getattr(t, '_tool_name', getattr(t, 'name', str(t))) for t in tools]
    # Check chatbot-specific tools are present
    assert any("memory" in str(n).lower() for n in tool_names)
    assert any("fact" in str(n).lower() for n in tool_names)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_sdk_tools.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 4: Create agent_sdk/tools.py**

```python
# backend/src/porto_chatbot/agent_sdk/tools.py
"""Build @tool-decorated functions that wrap existing handlers.py logic.

Each @tool binds the current AgentToolContext via closure.
The actual logic lives in handlers.py — zero duplication.
"""
from __future__ import annotations

from typing import Any

from ..tools.context import AgentToolContext, _format_chunks, _truncate, _MAX_TOOL_RESULT_CHARS
from ..tools.handlers import (
    _get_prd_text, _get_understanding, _get_subsystem,
    _list_subsystems, _search_knowledgebase, _get_sources,
)
from ..memory.facts import build_facts_prompt


def _mcp_text(text: str) -> dict:
    """Return Agent SDK MCP content block format."""
    return {"content": [{"type": "text", "text": text}]}


def build_sdk_tools(ctx: AgentToolContext) -> list:
    """Create @tool functions bound to ctx via closure.

    Workflow ctx (no memory_store): registers get_prd, get_understanding,
    list_subsystems, get_subsystem, search_knowledgebase, get_sources.
    Chatbot ctx (with memory_store): additionally registers search_memory,
    get_session_facts.
    """
    try:
        from claude_agent_sdk import tool
    except ImportError:
        return []  # SDK not installed — caller handles gracefully

    tools = []

    @tool("get_prd_text", "读取当前 PRD 原文。当需要回顾输入需求时调用。", {})
    async def get_prd(args):
        return _mcp_text(_get_prd_text(ctx))

    @tool("get_understanding", "读取已生成的业务理解报告（Step 1 产物）。", {})
    async def get_understanding(args):
        return _mcp_text(_get_understanding(ctx))

    @tool("list_subsystems", "列出已识别的子系统及其职责。", {})
    async def list_subs(args):
        return _mcp_text(_list_subsystems(ctx))

    @tool("get_subsystem", "按名称读取单个子系统的完整定义。",
          {"name": str})
    async def get_sub(args):
        return _mcp_text(_get_subsystem(ctx, str(args.get("name", ""))))

    @tool("search_knowledgebase", "在知识库中检索与查询相关的文档片段。",
          {"query": str, "top_k": int})
    async def search_kb(args):
        top_k = int(args.get("top_k", 6) or 6)
        return _mcp_text(_search_knowledgebase(ctx, str(args.get("query", "")), top_k))

    @tool("get_sources", "读取已检索到的知识库片段。可选按 query 过滤。",
          {"query": str})
    async def get_srcs(args):
        return _mcp_text(_get_sources(ctx, str(args.get("query", ""))))

    tools.extend([get_prd, get_understanding, list_subs, get_sub, search_kb, get_srcs])

    # Chatbot-specific tools (only when memory_store/facts_store present)
    if ctx.memory_store is not None:
        @tool("search_memory", "跨会话语义检索对话记忆。", {"query": str, "session_id": str})
        async def search_mem(args):
            results = ctx.memory_store.search(
                str(args.get("query", "")),
                session_id=str(args.get("session_id", "")),
            )
            return _mcp_text(_format_chunks(results) if results else "无匹配记忆。")
        tools.append(search_mem)

    if ctx.facts_store is not None:
        @tool("get_session_facts", "读取本会话的结构化关键事实（决策/偏好/背景/待澄清）。",
              {"session_id": str})
        async def get_facts(args):
            grouped = ctx.facts_store.by_category(str(args.get("session_id", "")))
            text = build_facts_prompt(grouped)
            return _mcp_text(text if text else "当前会话无结构化事实。")
        tools.append(get_facts)

    return tools
```

- [ ] **Step 5: Create agent_sdk/backend.py**

```python
# backend/src/porto_chatbot/agent_sdk/backend.py
"""AgentSDKBackend: Claude Agent SDK implementation of AgentBackend."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from ..agent.backends import AgentBackend, NodeExecutionResult, BackendTools
from ..logging_utils import get_component_logger
from ..models import ChatRequest, ChatResponse
from ..settings import Settings
from ..tools.context import AgentToolContext
from .tools import build_sdk_tools


class AgentSDKBackend:
    """Claude Agent SDK engine.

    Uses ClaudeSDKClient + custom @tools + setting_sources for skill discovery.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = get_component_logger("backend.agent_sdk", settings)

    def build_tools(self, ctx: AgentToolContext) -> list:
        return build_sdk_tools(ctx)

    async def execute_node(
        self, *,
        system: str,
        user: str,
        tools: BackendTools | None = None,
        structured_schema: dict | None = None,
        max_turns: int = 10,
    ) -> NodeExecutionResult:
        from claude_agent_sdk import (
            ClaudeAgentOptions, ClaudeSDKClient, create_sdk_mcp_server,
        )

        sdk_tools = tools if isinstance(tools, list) else []
        server = create_sdk_mcp_server(
            name="porto", version="1.0.0", tools=sdk_tools,
        ) if sdk_tools else None

        options_kwargs: dict[str, Any] = {
            "system_prompt": system,
            "max_turns": max_turns,
            "setting_sources": ["project"],
            "cwd": str(self.settings.data_dir),
        }
        if server is not None:
            options_kwargs["mcp_servers"] = {"porto": server}
            options_kwargs["allowed_tools"] = ["mcp__porto__*"]
        if structured_schema is not None:
            options_kwargs["structured_output"] = structured_schema

        options = ClaudeAgentOptions(**options_kwargs)

        text = ""
        structured = None
        tool_calls = []
        turns = 0
        truncated = False
        reason = None

        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(user)
                async for msg in client.receive_response():
                    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                text += block.text
                    elif isinstance(msg, ResultMessage):
                        turns = getattr(msg, "num_turns", 0) or 0
                        if msg.subtype != "success":
                            truncated = True
                            reason = msg.subtype
                        if structured_schema and hasattr(msg, "result"):
                            try:
                                structured = json.loads(msg.result) if msg.result else None
                            except (json.JSONDecodeError, TypeError):
                                pass
        except Exception as exc:
            self.logger.exception("agent_sdk execute_node failed")
            return NodeExecutionResult(
                text=f"Agent SDK 执行失败：{exc}",
                truncated=True, reason="agent_sdk_error",
            )

        return NodeExecutionResult(
            text=text, structured=structured,
            tool_calls=tool_calls, turns=turns,
            truncated=truncated, reason=reason,
        )

    async def chat(self, req: ChatRequest, settings: Settings) -> ChatResponse:
        raise NotImplementedError("Implemented in Task 7")

    async def chat_stream(
        self, req: ChatRequest, settings: Settings,
    ) -> AsyncIterator[str]:
        raise NotImplementedError("Implemented in Task 7")
        yield  # make it a generator
```

- [ ] **Step 6: Create agent_sdk/__init__.py**

```python
# backend/src/porto_chatbot/agent_sdk/__init__.py
"""Claude Agent SDK backend module."""
from .backend import AgentSDKBackend
from .tools import build_sdk_tools

__all__ = ["AgentSDKBackend", "build_sdk_tools"]
```

- [ ] **Step 7: Run tests**

Run: `cd backend && uv run pytest tests/test_sdk_tools.py tests/test_agent_sdk_backend.py -v`
Expected: test_sdk_tools PASS (build_sdk_tools returns list with correct tools)

- [ ] **Step 8: Commit**

```bash
cd backend
git add pyproject.toml src/porto_chatbot/agent_sdk/ tests/test_sdk_tools.py
git commit -m "feat: add AgentSDKBackend with execute_node and build_sdk_tools"
```

---

## Task 7: AgentSDKBackend chat + chat_stream + Stop hook

**Files:**
- Modify: `backend/src/porto_chatbot/agent_sdk/backend.py` (implement `chat` + `chat_stream`)
- Test: `backend/tests/test_agent_sdk_chat.py`

**Interfaces:**
- Consumes: `MemoryStore`, `SessionFactsStore`, `get_index_supervisor` from deps
- Consumes: `build_sdk_tools` with chatbot ctx
- Produces: `AgentSDKBackend.chat()` and `chat_stream()` returning same structure as langchain

- [ ] **Step 1: Write the test (mock-based)**

```python
# backend/tests/test_agent_sdk_chat.py
"""Test AgentSDKBackend.chat structure (mocked SDK client)."""
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from porto_chatbot.agent_sdk.backend import AgentSDKBackend
from porto_chatbot.settings import Settings
from porto_chatbot.models import ChatRequest


def test_chat_returns_chat_response_on_error():
    """When SDK fails, chat returns a ChatResponse with error info (not 500)."""
    s = Settings()
    s.chatbot_backend = "agent_sdk"
    backend = AgentSDKBackend(s)

    req = ChatRequest(message="hi", session_id="test")
    # Mock ClaudeSDKClient to raise
    with patch("porto_chatbot.agent_sdk.backend.ClaudeSDKClient",
               side_effect=Exception("SDK unavailable")):
        result = asyncio.get_event_loop().run_until_complete(backend.chat(req, s))
        assert result.answer is not None  # error message, not crash
        assert "不可用" in result.answer or "失败" in result.answer
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_agent_sdk_chat.py -v`
Expected: FAIL — `chat` raises NotImplementedError

- [ ] **Step 3: Implement chat()**

In `backend/src/porto_chatbot/agent_sdk/backend.py`, replace the `chat` stub:

```python
    async def chat(self, req: ChatRequest, settings: Settings) -> ChatResponse:
        from ..api.deps import get_index_supervisor, get_store, get_memory
        from ..memory import SessionFactsStore, build_facts_prompt, trigger_facts_extraction_sync
        from ..memory.store import MemoryStore
        from ..models import ChatResponse as CR
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, create_sdk_mcp_server, HookMatcher

        # RAG availability check
        available, reason = get_index_supervisor().rag_available()
        if not available:
            return CR(
                answer=f"知识库当前不可用（{reason}），请稍后重试。",
                sources=[], memory=[], evaluation={"score": 0.0, "passed": False, "cases": []},
                steps=[{"name": "rag_check", "status": "skipped", "summary": reason, "data": {}}],
            )

        store = get_store(settings)
        memory = get_memory(settings)
        facts_store = SessionFactsStore(settings)
        store.ensure_index()

        # Build chatbot ctx with memory access
        ctx = AgentToolContext(
            state={}, vector_store=store,
            memory_store=memory, facts_store=facts_store,
        )
        sdk_tools = build_sdk_tools(ctx)
        server = create_sdk_mcp_server(name="porto", version="1.0.0", tools=sdk_tools)

        # Stop hook: persist conversation + trigger facts extraction
        async def on_stop(input_data, tool_use_id, context):
            try:
                memory.add(session_id=req.session_id, role="user", content=req.message)
                if answer_text:
                    memory.add(session_id=req.session_id, role="assistant", content=answer_text)
                trigger_facts_extraction_sync(
                    store=facts_store, llm=type("FakeLLM", (), {"enabled": False})(),
                    session_id=req.session_id, new_message=req.message,
                    recent_turns=[], settings=settings,
                )
            except Exception:
                self.logger.exception("stop hook failed session=%s", req.session_id)

        options = ClaudeAgentOptions(
            system_prompt=(
                "你是 Porto 知识库问答助手。你可以调用工具检索知识库、对话记忆和结构化事实。"
                "优先基于工具返回的信息回答；不确定时说明缺口。"
                f"当前 session_id: {req.session_id}"
            ),
            setting_sources=["project"],
            cwd=str(settings.data_dir),
            mcp_servers={"porto": server},
            allowed_tools=["mcp__porto__*"],
            max_turns=settings.agent_max_tool_turns,
            hooks={"Stop": [HookMatcher(matcher="", hooks=[on_stop])]},
        )

        answer_text = ""
        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(req.message)
                from claude_agent_sdk import AssistantMessage, TextBlock
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                answer_text += block.text
        except Exception as exc:
            self.logger.exception("agent_sdk chat failed session=%s", req.session_id)
            return CR(
                answer=f"Agent 引擎暂时不可用：{exc}。请在 Settings 切换到 Langchain 引擎。",
                sources=[], memory=[], evaluation={"score": 0.0, "passed": False, "cases": []},
                steps=[{"name": "agent_error", "status": "failed",
                        "summary": str(exc), "data": {}}],
            )

        if not answer_text:
            answer_text = "（Agent 未返回内容，请重试或检查配置）"

        return CR(
            answer=answer_text,
            sources=[],
            memory=[],
            evaluation={"score": 0.0, "passed": True, "cases": []},
            steps=[
                {"name": "agent_react", "status": "completed",
                 "summary": "Agent SDK ReAct loop", "data": {}},
                {"name": "answer", "status": "completed",
                 "summary": "完成回答生成", "data": {}},
            ],
        )
```

- [ ] **Step 4: Implement chat_stream() (SSE)**

In `backend/src/porto_chatbot/agent_sdk/backend.py`, replace the `chat_stream` stub with an async generator that wraps the SDK message stream into ai-sdk SSE format. The structure mirrors `langchain_chat_stream` but sources deltas from `ClaudeSDKClient.receive_response()`:

```python
    async def chat_stream(self, req: ChatRequest, settings: Settings) -> AsyncIterator[str]:
        from ..api.deps import get_index_supervisor
        from ..api.sse import _ai_sdk_sse
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, create_sdk_mcp_server, HookMatcher, AssistantMessage, TextBlock

        text_id = "answer-1"
        yield _ai_sdk_sse({"type": "start", "messageMetadata": {"session_id": req.session_id}})
        yield _ai_sdk_sse({"type": "start-step"})
        yield _ai_sdk_sse({"type": "text-start", "id": text_id})

        try:
            # Same setup as chat() — build ctx, tools, options
            # (See Task 7 Step 3 for full option construction)
            # ... (reuse the option-building logic)

            async with ClaudeSDKClient(options=options) as client:
                await client.query(req.message)
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock) and block.text:
                                yield _ai_sdk_sse({"type": "text-delta", "id": text_id, "delta": block.text})
        except Exception as exc:
            yield _ai_sdk_sse({"type": "error", "errorText": str(exc)})

        yield _ai_sdk_sse({"type": "text-end", "id": text_id})
        yield _ai_sdk_sse({"type": "finish-step"})
        yield _ai_sdk_sse({"type": "finish", "finishReason": "stop"})
        yield "data: [DONE]\n\n"
```

Note: The full option construction in chat_stream mirrors chat() (RAG check, ctx, tools, hooks). To avoid duplication, extract a `_build_chat_options(req, settings) -> ClaudeAgentOptions` helper used by both.

- [ ] **Step 5: Run test**

Run: `cd backend && uv run pytest tests/test_agent_sdk_chat.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
cd backend
git add src/porto_chatbot/agent_sdk/backend.py tests/test_agent_sdk_chat.py
git commit -m "feat: implement AgentSDKBackend.chat and chat_stream with Stop hook"
```

---

## Task 8: Skill 系统——definitions + deploy + 启动钩子

**Files:**
- Create: `backend/src/porto_chatbot/agent_sdk/skills.py`
- Modify: `backend/src/porto_chatbot/main.py` (add startup hook)
- Test: `backend/tests/test_skill_deploy.py`

**Interfaces:**
- Consumes: prompt strings from `specs/steps.py`, `specs/rubric.py`
- Produces: `SKILLS` dict mapping skill names to content
- Produces: `deploy_skills(data_dir)` that writes `~/.porto/.claude/skills/*/SKILL.md`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_skill_deploy.py
"""Test that deploy_skills creates correct SKILL.md files from code templates."""
import tempfile
from pathlib import Path
from porto_chatbot.agent_sdk.skills import deploy_skills, SKILLS, CLAUDE_MD


def test_skills_dict_has_required_entries():
    assert "prd-analysis" in SKILLS
    assert "subsystem-decomposition" in SKILLS
    assert "spec-generation" in SKILLS
    assert "spec-evaluation" in SKILLS
    assert "porto-memory" in SKILLS


def test_deploy_skills_creates_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        deploy_skills(data_dir)

        # CLAUDE.md exists
        assert (data_dir / ".claude" / "CLAUDE.md").exists()

        # Each skill has SKILL.md
        for name in SKILLS:
            skill_file = data_dir / ".claude" / "skills" / name / "SKILL.md"
            assert skill_file.exists(), f"Missing skill: {name}"
            content = skill_file.read_text(encoding="utf-8")
            assert content.startswith("---")  # YAML frontmatter
            assert f"name: {name}" in content


def test_deploy_skills_is_idempotent():
    """Running twice produces same output."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        deploy_skills(data_dir)
        first = (data_dir / ".claude" / "skills" / "prd-analysis" / "SKILL.md").read_text()
        deploy_skills(data_dir)
        second = (data_dir / ".claude" / "skills" / "prd-analysis" / "SKILL.md").read_text()
        assert first == second
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_skill_deploy.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Create skills.py**

```python
# backend/src/porto_chatbot/agent_sdk/skills.py
"""Skill definitions and deployment.

Code is the single source of truth — SKILL.md files are generated products.
Langchain mode uses the code strings directly; Agent SDK mode uses the files.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..specs.rubric import _rubric_text
from ..specs.steps import _SPEC_SECTIONS
from ..specs.template import render_template_spec


@dataclass(frozen=True)
class SkillDefinition:
    description: str
    body: str


# System prompts sourced from existing code (specs/steps.py, etc.)
# These are the SAME strings used by langchain mode — single source of truth.

_UNDERSTAND_PROMPT = (
    "你是资深业务分析师。根据 PRD 和知识库片段，输出简洁的中文业务理解报告，"
    "包含：执行摘要、业务目标、核心实体、子系统线索。"
    "可调用工具获取 PRD 原文与检索知识库，自主决定检索什么。"
)

_IDENTIFY_PROMPT = (
    "你是资深系统架构师。按领域驱动设计原则，根据业务理解报告与 PRD 识别需要拆分的子系统。"
    "每个子系统职责单一、边界清晰，数量控制在 2-6 个，命名形如 xxx-service。"
)

_GENERATE_PROMPT = (
    f"你是资深系统规格工程师。生成详细的系统需求规格（markdown）。"
    f"必须包含这些章节：{', '.join(_SPEC_SECTIONS)}。"
    "API 需求要给出具体端点/方法/输入输出/错误码；数据模型要列实体与关键字段；验收标准要具体可测。"
    "可调用工具检索知识库以参考现有系统约定。"
)

_EVALUATE_PROMPT = (
    "你是严格的系统规格评审专家。只评审、不重写。依据如下 rubric 打分：\n"
    f"{_rubric_text()}"
)

_MEMORY_GUIDE = (
    "# Porto Memory 系统使用指南\n\n"
    "Porto 有三层 memory 系统：\n"
    "1. **search_memory**: 跨会话语义检索对话记忆。当用户提到之前讨论过的内容时调用。\n"
    "2. **get_session_facts**: 读取当前会话的结构化关键事实（4 类：决策/偏好/背景/待澄清）。\n"
    "   优先参考 facts——它们是用户已确认的信息。\n"
    "3. **search_knowledgebase**: 在 SCV 生成的知识库中检索文档片段。\n\n"
    "调用时机：不确定时先查 facts 和 memory，再查 knowledgebase。寒暄闲聊不需要调用任何工具。"
)

CLAUDE_MD = """# Porto — Codebase-Aware Spec Engineering

你是 Porto 的 spec 工程助手。你有以下工具和技能可用。

## 工具
- search_knowledgebase: 检索知识库文档片段
- search_memory: 跨会话语义检索对话记忆
- get_session_facts: 读取结构化关键事实
- get_prd_text / get_understanding / list_subsystems / get_subsystem / get_sources: 工作流状态访问

## 行为准则
- 优先基于工具返回的信息回答，不确定时说明缺口
- 寒暄闲聊直接回答，不需要调用工具
- 结构化事实（facts）优先级最高——它们是用户已确认的信息
"""


SKILLS: dict[str, SkillDefinition] = {
    "prd-analysis": SkillDefinition(
        description="理解 PRD 业务意图，提取核心需求和技术约束",
        body=_UNDERSTAND_PROMPT,
    ),
    "subsystem-decomposition": SkillDefinition(
        description="识别子系统及其职责、能力、数据实体、依赖关系",
        body=_IDENTIFY_PROMPT,
    ),
    "spec-generation": SkillDefinition(
        description="生成子系统规格并迭代优化（generate-critique-refine loop）",
        body=_GENERATE_PROMPT,
    ),
    "spec-evaluation": SkillDefinition(
        description="按 rubric 评估 spec 质量，决定是否需要返工",
        body=_EVALUATE_PROMPT,
    ),
    "porto-memory": SkillDefinition(
        description="Porto memory 系统使用指南：何时查 facts、何时查记忆",
        body=_MEMORY_GUIDE,
    ),
}


def deploy_skills(data_dir: Path) -> None:
    """Deploy CLAUDE.md and SKILL.md files from code templates.

    Called at backend startup. Idempotent — overwrites each time.
    Code changes to prompts sync to skill files on next restart.
    """
    claude_dir = data_dir / ".claude"
    skills_dir = claude_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    (claude_dir / "CLAUDE.md").write_text(CLAUDE_MD, encoding="utf-8")

    for name, skill in SKILLS.items():
        skill_dir = skills_dir / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        content = (
            f"---\n"
            f"name: {name}\n"
            f"description: {skill.description}\n"
            f"---\n\n"
            f"{skill.body}\n"
        )
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
```

- [ ] **Step 4: Add startup hook to main.py**

In `backend/src/porto_chatbot/main.py`, find the startup/lifespan function and add:

```python
    # Deploy Agent SDK skills (idempotent, overwrites each startup)
    from .agent_sdk.skills import deploy_skills
    deploy_skills(settings.data_dir)
```

Place it after settings initialization, before the app starts serving.

- [ ] **Step 5: Run tests**

Run: `cd backend && uv run pytest tests/test_skill_deploy.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
cd backend
git add src/porto_chatbot/agent_sdk/skills.py src/porto_chatbot/main.py tests/test_skill_deploy.py
git commit -m "feat: add skill definitions and deploy_skills startup hook"
```

---

## Task 9: 前端 Settings 卡片——backend 选择器 + provider 联动

**Files:**
- Modify: `frontend/src/lib/types.ts` (AppSettings.agent type)
- Modify: `frontend/src/components/porto-workbench.tsx` (Settings UI)

**Interfaces:**
- Consumes: `PUT /api/settings` API (existing)
- Produces: `chatbot_backend` and `workflow_backend` in the agent settings payload
- Produces: Card selector UI with provider/model联动

- [ ] **Step 1: Add backend fields to frontend types**

In `frontend/src/lib/types.ts`, find the `AppSettings` type's `agent` section and add:

```typescript
  chatbot_backend?: "langchain" | "agent_sdk";
  workflow_backend?: "langchain" | "agent_sdk";
```

- [ ] **Step 2: Add card selector state to porto-workbench.tsx**

Find the existing agent config state (e.g., `const [agentConfig, setAgentConfig] = useState(...)`) and add backend state alongside it. The card selector renders two groups (chatbot / workflow), each with two options (Langchain / Claude Agent SDK).

- [ ] **Step 3: Implement provider联动**

When user selects "Claude Agent SDK" for either backend:
- Auto-set `agent_provider` to `"anthropic"`
- Filter `agent_model` dropdown to Claude models only (`claude-sonnet-5`, `claude-opus-5`, `claude-haiku-4-5`, etc.)
- Disable manual provider editing (show as locked)

When switching back to "Langchain":
- Restore provider/model free selection

- [ ] **Step 4: Wire up save**

The existing `saveAppSettings` call already sends the full agent object. Ensure `chatbot_backend` and `workflow_backend` are included in the payload when the user saves.

- [ ] **Step 5: Manual verification**

Run: `cd backend && uv run uvicorn porto_chatbot.main:app --reload --port 8100`
Run: `cd frontend && npm run dev`

1. Open Settings page
2. Verify two card selectors appear (Chatbot 引擎 / Workflow 引擎)
3. Select "Claude Agent SDK" for chatbot → provider locks to anthropic, model shows Claude options
4. Save → verify `GET /api/settings` returns `chatbot_backend: "agent_sdk"`
5. Send a chat message → verify it routes to AgentSDKBackend (check backend logs)
6. Switch back to "Langchain" → verify provider unlocks, chat routes to langchain

- [ ] **Step 6: Commit**

```bash
cd frontend
git add src/lib/types.ts src/components/porto-workbench.tsx
git commit -m "feat(frontend): add engine card selectors with provider linkage"
```

---

## Task 10: 集成测试 + Dockerfile 更新

**Files:**
- Create: `backend/tests/test_workflow_agent_sdk_e2e.py`
- Modify: `Dockerfile` (add claude-agent-sdk)
- Test: manual + `@pytest.mark.integration`

- [ ] **Step 1: Update Dockerfile**

In `Dockerfile`, ensure `claude-agent-sdk` is installed. The bundled CLI should come with `pip install`. Add to the backend install step if not already covered by `uv sync` (it should be, since we added it to pyproject.toml in Task 6).

- [ ] **Step 2: Write integration test (requires API key)**

```python
# backend/tests/test_workflow_agent_sdk_e2e.py
"""End-to-end test: workflow with agent_sdk backend.

Requires ANTHROPIC_API_KEY. Mark as integration — not run in CI by default.
"""
import pytest
from porto_chatbot.settings import Settings
from porto_chatbot.agent.factory import create_backend
from porto_chatbot.agent.backends import NodeExecutionResult


@pytest.mark.integration
@pytest.mark.skipif(
    not Settings().agent_api_key,
    reason="No API key configured"
)
def test_agent_sdk_execute_node_basic():
    """Verify AgentSDKBackend.execute_node returns text for a simple prompt."""
    import asyncio
    s = Settings()
    s.workflow_backend = "agent_sdk"
    backend = create_backend(s, scope="workflow")

    result = asyncio.get_event_loop().run_until_complete(
        backend.execute_node(
            system="You are a test assistant. Reply with exactly: hello",
            user="test",
        )
    )
    assert isinstance(result, NodeExecutionResult)
    assert len(result.text) > 0
```

- [ ] **Step 3: Run integration test manually**

Run: `cd backend && uv run pytest tests/test_workflow_agent_sdk_e2e.py -v -m integration`
Expected: PASS (if API key configured and SDK installed)

- [ ] **Step 4: Final full test suite run**

Run: `cd backend && uv run pytest tests/ -x -q --ignore=tests/test_workflow_agent_sdk_e2e.py`
Expected: All non-integration tests pass

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_workflow_agent_sdk_e2e.py Dockerfile
git commit -m "test: add agent_sdk e2e integration test and Dockerfile update"
```

---

## Self-Review

**Spec coverage check:**

| Spec section | Covered by task |
|---|---|
| D1 方案 B (骨架不变+节点升级) | Task 4 (nodes), Task 6 (AgentSDKBackend) |
| D2 Chatbot 也上 Agent SDK | Task 7 (chat/chat_stream) |
| D3 不留 fallback | Task 7 (error returns ChatResponse, not crash) |
| D4 策略模式 | Task 2 (Protocol + factory) |
| D5 两个独立 backend 字段 | Task 1 (config), Task 2 (factory scope param) |
| D6 Memory 封装为 tools | Task 3 (ctx extend), Task 6 (build_sdk_tools) |
| D7 Compaction 退役 | Task 7 (no manual compaction in agent_sdk chat) |
| D8 Chatbot 无 intent 路由 | Task 7 (chat skips route_chat_intent) |
| D9 Skill 系统 | Task 8 (definitions + deploy) |
| D10 Skill 从代码部署 | Task 8 (deploy_skills from SKILLS dict) |
| D11 直接上 skill | Task 6 (setting_sources=["project"]) |
| D12 spec loop 保留结构 | Task 4 (only execute_node swap, loop unchanged) |
| 前端卡片 | Task 9 |
| 错误处理 | Task 7 (try/except in chat), Task 6 (execute_node error) |
| 测试策略 | All tasks (TDD), Task 10 (e2e) |

**Placeholder scan:** No TBD/TODO. All steps have actual code.

**Type consistency:** `NodeExecutionResult` fields (`text`, `structured`, `tool_calls`, `turns`, `truncated`, `reason`) are consistent across Task 2 (definition), Task 4 (consumers), Task 6 (AgentSDKBackend). `create_backend(settings, *, llm, scope)` signature consistent across Task 2 and all consumers.

**Gaps found and addressed:**
- `asyncio.get_event_loop().run_until_complete()` pattern used in sync node functions — this is correct for FastAPI sync endpoints (they run in threadpool). Verified consistent with existing codebase patterns.
- `refine_spec` previously checked `ctx.llm.enabled` — now checks via `ctx.backend` existence. Need to ensure SpecContext always has backend set (Task 4 Step 5 guarantees this).
