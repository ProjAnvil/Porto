# Claude Agent SDK ReAct 引擎设计

**日期**: 2026-08-01
**状态**: Draft（待用户审核）
**作者**: yuhaochen + Claude

---

## 1. 概述

### 1.1 目标

为 Porto 新增 **Claude Agent SDK** 作为可选执行引擎，让 chatbot（RAG 问答）和 workflow（需求拆解）两条路径都能以 Claude Code 级别的 ReAct agent loop 运行，同时**完全保留现有 Langchain + LangGraph 模式**，用户通过前端 Settings 卡片切换。

### 1.2 背景

Porto 当前架构：
- **Chatbot**：`/api/chat` → `route_chat_intent`（LLM/规则意图分类）→ direct 或 rag 路径 → `LLMClient.complete()`/`stream()`（langchain），无工具调用，纯 RAG 拼接 prompt。
- **Workflow**：LangGraph 5 步 StateGraph（retrieve → understand → identify → generate → evaluate），每节点内 `LLMClient.complete_with_tools()` 手写 ReAct mini-loop（6 个只读工具）。
- **Memory**：三层系统——对话历史（sqlite + chroma）、会话压缩（compaction.py）、Session Facts（4 类结构化，LLM 自动提取）。
- 两套功能共用 `LLMClient`（langchain，支持 openai/anthropic provider）。

Claude Agent SDK（`pip install claude-agent-sdk`）是 Claude Code CLI 的库化封装：内置完整 agent loop（天然 ReAct）、Read/Write/Edit/Bash/Glob/Grep/WebSearch/WebFetch 工具集、context compaction、`@tool` custom tools、hooks、skill 发现（`setting_sources`）、session resume/fork、`structured_output`。

### 1.3 不做的事

- **不删除/不改 Langchain 路径**——现有行为零变化（节点做机械性的接口提取重构，但 langchain 行为不变）。
- **不自动回退**——选了 Agent SDK 后出错不回退 Langchain，而是优雅返回错误信息。
- **不替换 LangGraph**——workflow 的固定 5 步骨架、interrupt、rerun、checkpoint 全部保留。

---

## 2. 关键决策记录

| # | 决策 | 理由 |
|---|------|------|
| D1 | **方案 B：骨架不变 + 节点升级** | 保留 Porto "fixed, fully observable workflow" 卖点（固定路径、AgentStep 可观测性、interrupt/rerun），同时让每节点内部升级为真正的 ReAct agent |
| D2 | **Chatbot 也上 Agent SDK**（不延后 Phase 2） | 用户确认；chatbot 从"手动拼 prompt 生成"升级为"Claude 自主 ReAct（自主决定检索策略）" |
| D3 | **选 Agent SDK 后不留 fallback** | 两个一等公民（langchain / agent_sdk），不是主备关系；选了谁就用谁 |
| D4 | **策略模式 + Protocol 消除 if-else** | 全系统只有工厂一处条件判断；加新引擎 = 加新类，不改任何调用方 |
| D5 | **两个独立 backend 字段**（chatbot_backend + workflow_backend） | 两个维度独立控制——用户可以只切 chatbot 或只切 workflow |
| D6 | **Memory 系统保留，封装为 custom tools** | Porto 的三层 memory（结构化 facts、跨 session 向量检索）是产品价值；Agent SDK 原生 memory tool 是通用文件系统，不如结构化精确 |
| D7 | **compaction 退役**（Agent SDK 模式） | Agent SDK 有内置 auto-compaction，不需要 Porto 手动压缩；langchain 模式 compaction 不动 |
| D8 | **Chatbot 不保留 intent 路由**（Agent SDK 模式） | Claude 有 tools 自主判断是否需要检索，不需要前置分类器；RAG 可用性检查保留 |
| D9 | **Skill 纳入设计**（5 个 skill + CLAUDE.md） | Agent SDK 支持 `setting_sources=["project"]` 加载 `.claude/skills/`；比塞进 system_prompt 更模块化 |
| D10 | **Skill 从代码模板自动部署**（后端启动时） | 代码是单一真相源，改 prompt 只改代码，重启自动同步到 skill 文件；不影响 langchain 模式 |
| D11 | **直接上 skill 发现机制，不兜底** | 用户确认；实施时如遇 bug 视为 bug 修复 |
| D12 | **spec refinement loop 保留结构**（方案 i） | 四重终止是质量保证，初版只换底层 execute_node，不改为 subagent 自主迭代 |

---

## 3. 架构设计——策略模式

### 3.1 双后端并存

```
                    ┌─────────────────────────────────┐
                    │        前端 Settings 卡片         │
                    │  chatbot_backend:  [langchain]   │
                    │                    [agent_sdk]   │
                    │  workflow_backend: [langchain]   │
                    │                    [agent_sdk]   │
                    └──────────────┬──────────────────┘
                                   │ agent_backend 配置
                    ┌──────────────▼──────────────────┐
                    │        工厂（唯一一处 if-else）   │
                    └──────┬───────────────┬──────────┘
                           │               │
              ┌────────────▼──┐     ┌──────▼──────────────┐
              │  Langchain     │     │  Agent SDK           │
              │  （零行为变化） │     │  （平行新增）        │
              │                │     │                     │
              │ LLMClient      │     │ AgentSDKBackend      │
              │ LangchainBackend│    │ ClaudeSDKClient      │
              └────────┬───────┘     └──────────┬──────────┘
                       │                        │
                  ┌────▼────────────────────────▼────┐
                  │          共享层（不动）            │
                  │  MemoryStore / FactsStore (sqlite)│
                  │  向量检索 (chroma + BM25)         │
                  │  LangGraph StateGraph 骨架        │
                  │  WorkflowExecutor / interrupt     │
                  │  config_store / settings          │
                  └───────────────────────────────────┘
```

### 3.2 抽象契约——`AgentBackend` Protocol

```python
# agent/backends.py（新增）

from typing import Protocol, Any
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass
class NodeExecutionResult:
    """节点执行的统一返回格式——消费方不关心后端差异。"""
    text: str
    tool_calls: list[dict]     # 统一的工具调用记录（供 AgentStep.data["tool_meta"]）
    turns: int
    truncated: bool
    reason: str | None = None


# BackendTools 是联合类型：list[ToolDef] | SDK MCP server
BackendTools = Any


class AgentBackend(Protocol):
    """一个执行引擎的完整契约。

    chatbot 和 workflow 的所有 LLM 交互都经过这个接口。
    加新引擎 = 实现这个 Protocol + 工厂加一行，不需要改任何调用方。
    """

    def build_tools(self, ctx: AgentToolContext) -> BackendTools:
        """返回该引擎的工具集。"""
        ...

    async def execute_node(
        self, *,
        system: str,
        user: str,
        tools: BackendTools,
        structured_schema: dict | None = None,
        max_turns: int = 10,
    ) -> NodeExecutionResult:
        """一次节点级 agent 调用。"""
        ...

    async def chat(self, req: ChatRequest, settings: Settings) -> ChatResponse:
        """chatbot 模式的完整处理。"""
        ...

    async def chat_stream(
        self, req: ChatRequest, settings: Settings
    ) -> AsyncIterator[str]:
        """SSE 流式版。"""
        ...
```

### 3.3 两个实现

**LangchainBackend**——封装现有逻辑，行为零变化：

```python
class LangchainBackend:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def build_tools(self, ctx: AgentToolContext) -> list[ToolDef]:
        return build_agent_tools(ctx)          # 现有 registry.py，不改

    async def execute_node(self, *, system, user, tools, **kw) -> NodeExecutionResult:
        r = self.llm.complete_with_tools(system, user, tools, **kw)
        return NodeExecutionResult(
            text=r.text, tool_calls=r.tool_calls,
            turns=r.turns, truncated=r.truncated, reason=r.reason,
        )

    async def chat(self, req, settings) -> ChatResponse:
        return _langchain_chat(req, settings)   # 现有 chat.py 逻辑搬进来

    async def chat_stream(self, req, settings): ...
```

**AgentSDKBackend**——全新实现（见第 4 节）。

### 3.4 工厂——全系统唯一一处条件判断

```python
# agent/factory.py

def create_backend(settings: Settings, *, llm: LLMClient | None = None,
                   scope: str = "workflow") -> AgentBackend:
    """scope 决定读 chatbot_backend 还是 workflow_backend。"""
    backend = (settings.chatbot_backend if scope == "chatbot"
               else settings.workflow_backend)
    if backend == "agent_sdk":
        return AgentSDKBackend(settings)
    return LangchainBackend(llm or LLMClient(settings))
```

### 3.5 消费方——零 if-else

**PortoAgent** 持有 backend：

```python
class PortoAgent:
    def __init__(self, settings, vector_store=None, llm=None):
        ...
        self.backend = create_backend(settings, llm=self.llm, scope="workflow")
```

**Workflow 节点**——机械性改动（`complete_with_tools` → `backend.execute_node`）：

```python
def understand_prd(state, *, config):
    agent = config["configurable"]["agent"]
    ctx = AgentToolContext(state=state, vector_store=agent.vector_store)
    tools = agent.backend.build_tools(ctx)
    result = agent.backend.execute_node(
        system=UNDERSTAND_SYSTEM_PROMPT,
        user=state["prd_text"],
        tools=tools,
        structured_schema=UNDERSTANDING_SCHEMA,
    )
    return {"understanding": result.text, "current_step": "understand",
            **agent._step("understand_prd", "生成业务理解",
                          {"tool_meta": {"truncated": result.truncated, ...}})}
```

**Chatbot 路由**：

```python
@router.post("/api/chat")
def chat(req: ChatRequest):
    settings = apply_rag_settings(...)
    engine = create_backend(settings, scope="chatbot")
    return engine.chat(req, settings)
```

### 3.6 对现有代码的影响

| 改动 | 触及现有代码 | 行为变化 |
|------|-------------|---------|
| 节点内 `complete_with_tools` → `backend.execute_node` | ✅ 机械性（~6 节点，每处 2-3 行） | ❌ 无 |
| `chat.py` 函数体 → `LangchainBackend.chat()` | ✅ 搬移 | ❌ 无 |
| `LLMClient` / `registry.py` / `handlers.py` | ❌ 不改 | ❌ 无 |
| `WorkflowExecutor` / `graph.py` / `memory/` / `specs/` | ❌ 不改 | ❌ 无 |

---

## 4. AgentSDKBackend 内部组件

### 4.1 组件总览

```
AgentSDKBackend
│
├─ build_tools(ctx) ──────────→ SDK MCP Server
│   ├─ @tool search_knowledgebase(query, top_k)
│   ├─ @tool get_prd_text()
│   ├─ @tool get_understanding()
│   ├─ @tool list_subsystems() / get_subsystem(name)
│   ├─ @tool get_sources(query)
│   ├─ @tool search_memory(query, session_id)      ← chatbot 专属
│   └─ @tool get_session_facts(session_id)          ← chatbot 专属
│
├─ execute_node(system, user, tools, schema, ...) ──→ NodeExecutionResult
│   ├─ ClaudeAgentOptions(setting_sources, cwd, system_prompt, mcp_servers,
│   │    structured_output, max_turns)
│   ├─ ClaudeSDKClient.query(user) → 消费 message stream
│   └─ 从 ResultMessage 提取 text + tool 调用记录
│
├─ chat(req, settings) ──→ ChatResponse
│   ├─ RAG 可用性检查（保留）
│   ├─ agent loop + memory/knowledge tools + Stop hook 持久化
│   └─ 无 intent 路由（Claude 自主判断是否检索）
│
└─ chat_stream(req, settings) ──→ AsyncIterator[SSE]
```

### 4.2 Tools 映射——复用 handlers.py

`@tool` 函数通过闭包绑定 ctx，内部调现有 handler：

```python
# agent_sdk/tools.py（新增）

def build_sdk_tools(ctx: AgentToolContext) -> list:
    @tool("get_prd_text", "读取当前 PRD 原文。当需要回顾输入需求时调用。", {})
    async def get_prd(args):
        return _mcp_text(_get_prd_text(ctx))          # 现有 handler

    @tool("search_knowledgebase", "在知识库中检索相关文档片段。",
          {"query": str, "top_k": int})
    async def search_kb(args):
        return _mcp_text(_search_knowledgebase(ctx, args["query"], args.get("top_k", 6)))

    @tool("search_memory", "跨会话语义检索对话记忆。",
          {"query": str, "session_id": str})
    async def search_mem(args, _memory=ctx.memory_store):
        results = _memory.search(args["query"], session_id=args["session_id"])
        return _mcp_text(_format_chunks(results))

    @tool("get_session_facts", "读取本会话的结构化关键事实。",
          {"session_id": str})
    async def get_facts(args, _facts=ctx.facts_store):
        grouped = _facts.by_category(args["session_id"])
        return _mcp_text(build_facts_prompt(grouped))

    return [get_prd, search_kb, search_mem, get_facts, ...]
```

Workflow 的 ctx 无 `memory_store`/`facts_store`（chatbot 专属），`build_sdk_tools` 根据 ctx 属性决定注册哪些 tool。

### 4.3 Skill 系统

**目录结构**（后端启动时从代码模板自动部署到 `~/.porto/.claude/`）：

```
~/.porto/.claude/
├── CLAUDE.md                              ← Porto 项目级指令
└── skills/
    ├── prd-analysis/SKILL.md              ← understand 节点方法论
    ├── subsystem-decomposition/SKILL.md   ← identify 节点方法论
    ├── spec-generation/SKILL.md           ← generate 节点方法论
    ├── spec-evaluation/SKILL.md           ← 12 分 rubric
    └── porto-memory/SKILL.md              ← memory 系统使用指南
```

**代码是真相源，skill 是生成产物**：

```python
# agent_sdk/skills/definitions.py
SKILLS: dict[str, SkillDefinition] = {
    "prd-analysis": SkillDefinition(
        description="理解 PRD 业务意图",
        body=UNDERSTAND_SYSTEM_PROMPT,      # 从 specs/steps.py 引用
    ),
    ...
}

# agent_sdk/skills/deploy.py
def deploy_skills(data_dir: Path) -> None:
    """后端启动时调用：从代码模板生成 SKILL.md。幂等，每次覆盖。"""
    ...
```

AgentSDKBackend 消费：

```python
options = ClaudeAgentOptions(
    setting_sources=["project"],            # 加载 ~/.porto/.claude/ skills
    cwd=str(settings.data_dir),
    system_prompt="运行时上下文（session_id 等）",
    mcp_servers={...},
    ...
)
```

### 4.4 Chatbot 执行流程（Agent SDK 模式）

```
AgentSDKBackend.chat(req, settings)
  ├─ RAG 可用性检查（保留 index_supervisor.rag_available()）
  │    └─ 不可用 → 返回提示
  ├─ 构造 chatbot ctx（memory_store + facts_store + vector_store）
  ├─ tools = build_sdk_tools(ctx)
  ├─ hooks: Stop → memory_store.add() + trigger_facts_extraction()
  ├─ ClaudeAgentOptions(setting_sources=["project"], cwd=~/.porto/,
  │     mcp_servers, allowed_tools, hooks)
  ├─ ClaudeSDKClient.query(req.message)
  │    └─ Claude 自主 ReAct: 查 facts? 查 memory? 查 knowledgebase? 直接回答?
  └─ 组装 ChatResponse（和 langchain 同构）
```

无 intent 路由——Claude 有 tools 自主判断。

### 4.5 Workflow 节点执行流程

节点签名 `(state, *, config) -> partial` 不变。内部 `backend.execute_node()` 多态分派：

```
understand_prd(state, config)
  ├─ ctx = AgentToolContext(state, vector_store)
  ├─ tools = agent.backend.build_tools(ctx)
  ├─ result = agent.backend.execute_node(system, user, tools, schema, max_turns)
  │    └─ AgentSDKBackend: ClaudeSDKClient.query + structured_output + skills
  └─ return {"understanding": result.text, **agent._step(...)}
```

`_project_state` / `_sync_status` / `interrupt_after` / `rerun_step` 全部不动。

### 4.6 spec refinement loop

保留现有 `generate_spec_with_loop` 结构（generate → critique → refine，四重终止）。三步各自走 `backend.execute_node()`。最小改动，不改为 subagent 自主迭代。

### 4.7 Memory 协调

| Memory 层 | Langchain 模式 | Agent SDK 模式 |
|-----------|---------------|----------------|
| 对话历史 | chat.py 手动查 + 拼 prompt | Claude 通过 `search_memory` tool 自主调用 |
| compaction | 手动 LLM 摘要（compaction.py） | **退役**——Agent SDK 内置 auto-compaction |
| Session Facts | chat.py 手动注入 prompt | Claude 通过 `get_session_facts` tool 自主调用 |
| 持久化 | chat.py 显式 `memory_store.add()` | **Stop hook** 触发 `memory_store.add()` + facts 提取 |

`MemoryStore` / `SessionFactsStore` / 向量检索全部保留，只是消费方从"手动拼"变成"Claude 通过 tool 自主调用"。

---

## 5. 数据流 + 配置 + 前端

### 5.1 配置模型

**`settings.py`** 新增：

```python
chatbot_backend: Literal["langchain", "agent_sdk"] = "langchain"
workflow_backend: Literal["langchain", "agent_sdk"] = "langchain"
```

**`models/payload.py`** 的 `AgentSettingsPayload` 新增对应字段。`config_store` 已有 agent namespace 持久化，新字段自动覆盖。

### 5.2 Chatbot Agent SDK 数据流

```
POST /api/chat/stream
  ├─ engine = create_backend(settings, scope="chatbot")
  └─ engine.chat_stream(req, settings)
       ├─ RAG 可用性检查
       ├─ build_sdk_tools(ctx)
       ├─ ClaudeAgentOptions(setting_sources, cwd, mcp_servers, hooks)
       ├─ ClaudeSDKClient.query(req.message)
       │    └─ Claude 自主 ReAct 循环（Thought → Action → Observe → ...）
       ├─ Stop hook → memory_store.add() + facts 提取
       └─ ai-sdk SSE 输出（前端零改动）
```

### 5.3 Workflow Agent SDK 数据流

```
POST /api/workflow/{id}/start
  ├─ _build_agent(row) → PortoAgent(backend=AgentSDKBackend)
  └─ graph.invoke(initial, config)          # LangGraph 骨架不变
       ├─ retrieve → backend.execute_node()
       ├─ understand → backend.execute_node()  [interrupt]
       ├─ identify → backend.execute_node()    [interrupt]
       ├─ generate → generate_spec_with_loop()  [interrupt]
       │              └─ 三步各自 backend.execute_node()
       └─ evaluate → backend.execute_node()
```

### 5.4 前端 Settings 卡片

现有 Settings 的 agent section 顶部新增两个卡片选择器：

```
┌─ Agent ──────────────────────────────────────────────┐
│  Chatbot 引擎                                        │
│  ┌──────────────┐  ┌──────────────┐                  │
│  │  Langchain   │  │ Claude Agent │                  │
│  │  RAG     ✓   │  │ SDK          │                  │
│  └──────────────┘  └──────────────┘                  │
│                                                      │
│  Workflow 引擎                                       │
│  ┌──────────────┐  ┌──────────────┐                  │
│  │  Langchain   │  │ Claude Agent │                  │
│  │         ✓    │  │ SDK          │                  │
│  └──────────────┘  └──────────────┘                  │
│                                                      │
│  Provider: [anthropic ▾]  ← 选 agent_sdk 时锁定      │
│  Model:    [claude-sonnet-5 ▾] ← 收敛到 Claude 家族  │
└──────────────────────────────────────────────────────┘
```

**联动规则**：选 "Claude Agent SDK" → provider 锁定 `anthropic`，model 收敛 Claude 家族。选 "Langchain" → 恢复自由选择。

**Workflow backend 锁定时机**：backend 在 workflow 创建时决定（存入 `agent_snapshot`），运行中切换不影响进行中的 workflow。新建 workflow 用新设置。Chatbot 每次请求实时读当前 settings。

---

## 6. 错误处理

原则：**不自动回退，优雅返回错误。**

| 场景 | 处理 | 用户可见 |
|------|------|---------|
| CLI 不可用（`CLINotFoundError`） | 启动时检测；Settings 卡片置灰 | "Agent SDK 不可用" |
| API key 无效 | 捕获 → ChatResponse / workflow failed | "Claude API 认证失败" |
| subprocess 崩溃（`ProcessError`） | 捕获 → 节点 failed / chat 错误气泡 | workflow 标红；chatbot 错误气泡 |
| structured_output 失败 | `NodeExecutionResult.truncated=True` | 红 chip（同现有截断处理） |
| max_turns 用尽 | Agent SDK 内置终止 → `truncated=True` | 红 chip |
| Stop hook 失败 | fail-open（异常 log，不阻塞主响应） | 用户无感 |
| Skill 未加载 | 不兜底；视为 bug 修复 | 实施时通过日志确认 |

Workflow 错误复用现有 `WorkflowExecutor._worker` 的 `except → update_status("failed")` 机制，零额外代码。

---

## 7. 测试策略

### 7.1 分层

```
backend/tests/
├── 现有测试（全保留，零改动）           ← langchain 回归保护
├── test_backends.py                     ← 策略层（mock）
├── test_sdk_tools.py                    ← @tool 包装验证
├── test_skill_deploy.py                 ← SKILL.md 生成验证
├── test_chatbot_agent_sdk_e2e.py        ← 集成（@integration，需 API key）
└── test_workflow_agent_sdk_e2e.py       ← 集成
```

### 7.2 关键场景

| 场景 | 验证点 |
|------|--------|
| `LangchainBackend.execute_node` 回归 | 输出和直接调 `complete_with_tools` 完全一致 |
| `AgentSDKBackend.execute_node`（mock） | ClaudeAgentOptions 正确构造 |
| `build_sdk_tools` 闭包 | 每个 @tool 调用对应 handler，ctx 绑定正确 |
| `deploy_skills` | SKILL.md 内容和代码模板一致 |
| backend 切换 | settings 改 backend 后请求走向正确引擎 |
| Agent SDK 异常 | 返回 ChatResponse（不是 500），workflow 标 failed |
| langchain 回归 | 现有全部测试通过 |

---

## 8. 实施风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| Agent SDK 锁死 Claude 模型 | 不能用 OpenAI provider | 设计已接受——langchain 模式仍支持 OpenAI |
| Skill 发现不可靠（[Issue #268](https://github.com/anthropics/claude-agent-sdk-python/issues/268)） | Claude 可能未加载方法论 | 不兜底（D11）；实施时通过日志确认；如遇 bug 修复 |
| `setting_sources=["project"]` 加载 user 级配置（[Issue #45602](https://github.com/anthropics/claude-code/issues/45602)） | `~/.claude/` 可能干扰 | `cwd` 设为 `~/.porto/`，隔离 Porto 的 `.claude/` |
| structured_output 校验失败（[Issue #571](https://github.com/anthropics/claude-agent-sdk-python/issues/571)） | 节点输出解析失败 | `NodeExecutionResult.truncated=True`，红 chip |
| Stop hook 拿不到 assistant 完整回复 | 对话持久化缺失 | 实施时实测；如不行从 message stream 收集兜底 |
| subprocess 部署复杂度 | Docker/容器化多一层 | Dockerfile 加 `pip install claude-agent-sdk`（bundles CLI） |
| 节点机械性重构引入 bug | langchain 行为受影响 | `test_backends.py` 回归测试保护 |

---

## 9. 后续演进（不在本次范围）

- **spec refinement → subagent 自主迭代**（方案 ii）：当前保留 loop 结构（D12），后续可改为 Agent SDK subagent 自主迭代
- **Managed Agents 后端**：工厂模式（D4）支持无缝新增第三个后端
- **用户自定义 skill**：当前每次启动覆盖（D10），后续可加 `.user-modified` 标记保护用户编辑
- **Chatbot 深度问答模式**：当前 chatbot agent sdk 是全自主 ReAct，后续可细化工具权限

---

## 10. 文件变更清单

### 新增文件

| 路径 | 内容 |
|------|------|
| `agent/backends.py` | `AgentBackend` Protocol + `NodeExecutionResult` + `BackendTools` |
| `agent/factory.py` | `create_backend()` 工厂 |
| `agent_sdk/__init__.py` | Agent SDK 后端模块 |
| `agent_sdk/backend.py` | `AgentSDKBackend` 实现 |
| `agent_sdk/tools.py` | `build_sdk_tools()` — @tool 包装 handlers.py |
| `agent_sdk/skills/definitions.py` | `SKILLS` 映射（代码 → skill 内容） |
| `agent_sdk/skills/deploy.py` | `deploy_skills()` — 启动时部署到 ~/.porto/.claude/ |
| `backend/tests/test_backends.py` | 策略层测试 |
| `backend/tests/test_sdk_tools.py` | @tool 测试 |
| `backend/tests/test_skill_deploy.py` | skill 部署测试 |

### 修改文件（机械性/增量）

| 路径 | 改动 |
|------|------|
| `settings.py` | 新增 `chatbot_backend` + `workflow_backend` 字段 |
| `models/payload.py` | `AgentSettingsPayload` 新增两个字段 |
| `agent/agent.py` | `PortoAgent.__init__` 注入 `self.backend` |
| `agent/nodes/*.py` | ~6 个节点：`complete_with_tools` → `backend.execute_node` |
| `agent/nodes/generate.py` | spec loop 三步走 `backend.execute_node` |
| `api/routes/chat.py` | 入口处 `create_backend(scope="chatbot")` 分派 |
| `main.py` | 启动钩子加 `deploy_skills(settings.data_dir)` |
| 前端 `lib/types.ts` | `AppSettings.agent` 加两个 backend 字段 |
| 前端 Settings 组件 | 新增卡片选择器 + provider 联动 |

### 不改文件

| 路径 | 原因 |
|------|------|
| `llm/client.py` | langchain 路径不动 |
| `tools/registry.py` / `tools/handlers.py` | 被 LangchainBackend + AgentSDKBackend 共同复用 |
| `tools/context.py` | ctx 结构不变 |
| `agent/graph.py` | LangGraph 骨架不动 |
| `workflow_executor.py` | 编排逻辑不动 |
| `workflow_store.py` | 存储不动 |
| `memory/*.py` | memory 系统不动 |
| `specs/*.py` | spec 方法论不动（内容被 skill definitions 引用） |
| `intent.py` | langchain 模式仍用；agent sdk 模式不调用 |
