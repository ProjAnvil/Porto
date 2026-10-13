# Orchestration Settings 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Langchain 聊天管道的编排层开放为「编排」设置页（sidebar 重组 + 意图引擎可插拔 + TypeSafe 接入 + agent_sdk 锁定态），并升级后端依赖、搭 Playwright E2E。

**Architecture:** 后端在 `intent.py` 单点扩展意图引擎注册逻辑（builtin_llm/rule/typesafe + 置信度回退），新设置键挂 `rag_chat` namespace（ConfigStore merge-then-save 保证部分更新安全），新增 `POST /api/intent/test`；前端在 `porto-workbench.tsx` 内新增编排组件群并把 sidebar 重组为 4 分组；Playwright 从零搭建双 webServer 冒烟套件。

**Tech Stack:** FastAPI + Pydantic + SQLite（backend）｜Next.js 16 + React 19 + Tailwind v4（frontend，无组件库）｜typesafe-sdk 0.7.4｜Playwright

**Spec:** `docs/superpowers/specs/2026-10-13-orchestration-settings.md`

## Global Constraints

- 意图引擎默认 `builtin_llm`，上线零行为变化；`route_chat_intent` 签名向后兼容（新参数带默认值）
- 新设置键全部在 `rag_chat` namespace：`chat_intent_engine`、`chat_typesafe_api_key`、`chat_typesafe_base_url`、`chat_typesafe_timeout_ms`(100–30000, 默认 2000)、`chat_typesafe_confidence_threshold`(0.0–1.0, 默认 0.6)、`chat_typesafe_low_confidence_fallback`(默认 True)
- `chat_typesafe_api_key` 加入 `SENSITIVE_SETTING_KEYS`
- 编排页仅 Langchain 生效；agent_sdk 下置灰 + 横幅，不清空已存配置
- UI 文案中文；视觉沿用 zinc 黑白灰 + `SettingsCard`/`EngineCardGroup`/`StrategyCardGroup` 既有语言
- 依赖：不引入 `langchain-typesafe`（alpha）；直接依赖 `typesafe-sdk>=0.7.4`，代码内 lazy import
- 每个任务以 `uv run pytest`（backend）/`npx playwright test`（frontend）验收；conventional commits

---

### Task 1: 依赖升级（langchain 系 + claude-agent-sdk + anthropic + 新增 typesafe-sdk）

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`（由 uv 生成）

**Interfaces:**
- Produces: 升级后的运行环境（后续任务依赖）；`typesafe-sdk` 可 import

- [ ] **Step 1: pyproject 增 typesafe-sdk，抬 langchain/anthropic 下限**

`backend/pyproject.toml` dependencies 中：`"claude-agent-sdk>=0.2.128"` → `">=0.2.165"`；`"anthropic>=0.60.0"` → `">=1.13.0"`；`langchain-core>=0.3.0` → `">=1.6.10"`；`langchain-anthropic>=0.3.0` → `">=1.7.6"`；`langchain-openai>=0.3.0` → `">=1.7.1"`；`langchain-text-splitters>=1.0.0` → `">=1.1.3"`；`langgraph>=0.4.0` → `">=1.2.15"`；新增 `"typesafe-sdk>=0.7.4"`。

- [ ] **Step 2: 重新解析锁文件**

```bash
cd backend && uv lock --upgrade-package langchain-core --upgrade-package langchain-anthropic \
  --upgrade-package langchain-openai --upgrade-package langchain-text-splitters \
  --upgrade-package langgraph --upgrade-package langgraph-checkpoint-sqlite \
  --upgrade-package claude-agent-sdk --upgrade-package anthropic && uv sync
```

若 anthropic 1.x 与 `llama-index-llms-anthropic` 冲突导致解析失败：回退 anthropic 为 `>=0.116.0`，锁定 0.x 最新，并在提交信息注明原因。

- [ ] **Step 3: 全量回归**

```bash
cd backend && uv run pytest -q
```

预期：全绿。出现弃用告警不阻塞，但记录到最终报告。

- [ ] **Step 4: Commit**

```bash
git add backend/pyproject.toml backend/uv.lock
git commit -m "chore(deps): upgrade langchain suite, claude-agent-sdk, anthropic; add typesafe-sdk"
```

---

### Task 2: 后端设置键 + 敏感键

**Files:**
- Modify: `backend/src/porto_chatbot/models/enums.py`（新增 `IntentEngine`）
- Modify: `backend/src/porto_chatbot/settings.py`（6 个新字段）
- Modify: `backend/src/porto_chatbot/models/payload.py:90-96`（`RagChatSettingsPayload` 扩展）
- Modify: `backend/src/porto_chatbot/config_store.py:78`（`RAG_CHAT_SETTING_KEYS`）、`:90`（`SENSITIVE_SETTING_KEYS`）
- Modify: `backend/src/porto_chatbot/api/deps.py:156-164`（`default_rag_chat_settings`）
- Test: `backend/tests/test_settings_payloads.py`（新建/追加）

**Interfaces:**
- Produces: `IntentEngine` 枚举（`builtin_llm`/`rule`/`typesafe`）；`Settings.chat_intent_engine` 等 6 字段；`RagChatSettingsPayload` 对应 `| None` 字段；ConfigStore 可持久化这些键

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_settings_payloads.py（追加）
from porto_chatbot.config_store import RAG_CHAT_SETTING_KEYS, SENSITIVE_SETTING_KEYS
from porto_chatbot.models import RagChatSettingsPayload
from porto_chatbot.models.enums import IntentEngine
from porto_chatbot.settings import Settings

def test_intent_engine_defaults_to_builtin_llm():
    s = Settings()
    assert s.chat_intent_engine == IntentEngine.BUILTIN_LLM
    assert s.chat_typesafe_timeout_ms == 2000
    assert s.chat_typesafe_confidence_threshold == 0.6
    assert s.chat_typesafe_low_confidence_fallback is True
    assert s.chat_typesafe_api_key is None

def test_rag_chat_keys_cover_typesafe_settings():
    assert {"chat_intent_engine", "chat_typesafe_api_key", "chat_typesafe_base_url",
            "chat_typesafe_timeout_ms", "chat_typesafe_confidence_threshold",
            "chat_typesafe_low_confidence_fallback"} <= RAG_CHAT_SETTING_KEYS
    assert "chat_typesafe_api_key" in SENSITIVE_SETTING_KEYS

def test_rag_chat_payload_partial_roundtrip(tmp_path):
    p = RagChatSettingsPayload(intent_engine="typesafe")
    assert p.chat_typesafe_api_key is None  # 部分更新不强制填其他字段
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && uv run pytest tests/test_settings_payloads.py -q
```

预期：FAIL（ImportError: IntentEngine）

- [ ] **Step 3: 实现**

`models/enums.py` 在 `IntentRoutingMode` 后加：

```python
class IntentEngine(StrEnum):
    BUILTIN_LLM = "builtin_llm"
    RULE = "rule"
    TYPESAFE = "typesafe"
```

`settings.py` 在 `chat_intent_routing_mode` 附近加：

```python
chat_intent_engine: IntentEngine = IntentEngine.BUILTIN_LLM
chat_typesafe_api_key: str | None = None
chat_typesafe_base_url: str | None = None
chat_typesafe_timeout_ms: int = Field(default=2000, ge=100, le=30000)
chat_typesafe_confidence_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
chat_typesafe_low_confidence_fallback: bool = True
```

`models/payload.py` `RagChatSettingsPayload` 追加（同约束 Field）：

```python
intent_engine: IntentEngine | None = None
chat_typesafe_api_key: str | None = None
chat_typesafe_base_url: str | None = None
chat_typesafe_timeout_ms: int | None = Field(default=None, ge=100, le=30000)
chat_typesafe_confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
chat_typesafe_low_confidence_fallback: bool | None = None
```

注意：payload 字段名与 Settings 字段名一致（不带 `chat_` 前缀差异），`default_rag_chat_settings` 逐字段映射：

```python
intent_engine=s.chat_intent_engine,
chat_typesafe_api_key=s.chat_typesafe_api_key,
chat_typesafe_base_url=s.chat_typesafe_base_url,
chat_typesafe_timeout_ms=s.chat_typesafe_timeout_ms,
chat_typesafe_confidence_threshold=s.chat_typesafe_confidence_threshold,
chat_typesafe_low_confidence_fallback=s.chat_typesafe_low_confidence_fallback,
```

`RAG_CHAT_SETTING_KEYS` 增加上述 6 键名，**并补 `"condense_enabled"`**（审计 BLOCKER：该键在 payload 中存在但键集遗漏，当前保存后读不回、GET 恒返回 env 默认——顺带修复的存量 bug）；`SENSITIVE_SETTING_KEYS` 增 `"chat_typesafe_api_key"`。测试同步断言 `"condense_enabled" in RAG_CHAT_SETTING_KEYS`。

- [ ] **Step 4: 跑测试通过 + 全量回归**

```bash
cd backend && uv run pytest tests/test_settings_payloads.py -q && uv run pytest -q
```

- [ ] **Step 5: Commit**

```bash
git add -A backend && git commit -m "feat(settings): intent engine and typesafe config keys in rag_chat namespace"
```

---

### Task 3: IntentDecision 扩展 + rule/builtin 引擎分支

**Files:**
- Modify: `backend/src/porto_chatbot/intent.py`
- Test: `backend/tests/test_intent.py`（追加）

**Interfaces:**
- Produces: `IntentDecision(intent, reason, confidence: float|None = None, engine: str = "", fallback_from: str|None = None)`；`route_chat_intent(message, settings=None, llm=None, routing_mode=BINARY, engine: IntentEngine|None = None)`
- 行为契约：engine 缺省 → `settings.chat_intent_engine`；`rule` → 纯规则；`builtin_llm` → 现 LLM 优先 + 规则回退（engine 字段按实际胜者填 `builtin_llm`/`rule`）；`off` → engine=""

- [ ] **Step 1: 失败测试**

```python
# backend/tests/test_intent.py（追加；审计修正：无 no_llm_settings fixture，
# conftest.py 已隔离 env/LLM key，直接 Settings() 即确定性；或传 settings=None）
from porto_chatbot.settings import Settings

def test_rule_engine_skips_llm():
    d = route_chat_intent("你好", Settings(), llm=None,
                          routing_mode=IntentRoutingMode.BINARY, engine=IntentEngine.RULE)
    assert d.intent == ChatIntent.DIRECT
    assert d.engine == "rule"
    assert d.confidence is None

def test_default_engine_is_builtin_llm():
    d = route_chat_intent("帮我查知识库里的支付文档", Settings(), llm=None)
    assert d.engine in ("builtin_llm", "rule")  # 无 llm 时降级 rule
```

- [ ] **Step 2: 确认失败** `uv run pytest tests/test_intent.py -q`（TypeError: unexpected keyword engine）

- [ ] **Step 3: 实现**

`route_chat_intent` 签名加 `engine: IntentEngine | None = None`；函数体：

```python
if routing_mode == IntentRoutingMode.OFF:
    return IntentDecision(ChatIntent.RAG, "routing_off", engine="")
engine = engine or (settings.chat_intent_engine if settings else IntentEngine.BUILTIN_LLM)
if engine == IntentEngine.RULE:
    decision = _rule_route(message, routing_mode)
    ...log...
    return replace(decision, engine="rule")
# builtin_llm / typesafe 走原 LLM 优先路径（typesafe 在 Task 4 插入）
```

`_llm_route` 胜出时 `engine="builtin_llm"`；规则回退时 `engine="rule"`。用 `dataclasses.replace` 或直接构造（frozen dataclass 用 replace）。现有测试的 `IntentDecision(intent, reason)` 双参构造因新字段带默认值保持兼容。

- [ ] **Step 4: 通过 + 全量**（现有 test_intent/test_chat_dispatch 不改应全绿）

- [ ] **Step 5: Commit** `feat(intent): pluggable intent engine field with rule/builtin branches`

---

### Task 4: TypeSafe 引擎（置信度 + 回退 + 降级）

**Files:**
- Modify: `backend/src/porto_chatbot/intent.py`（`_typesafe_route` + registry 常量 `INTENT_ENGINES` 供文档化）
- Test: `backend/tests/test_intent_typesafe.py`（新建）

**Interfaces:**
- Produces: `_typesafe_route(message, settings, llm, routing_mode) -> IntentDecision`
- 依赖注入点：`_build_typesafe_client(settings)` 工厂函数（测试 monkeypatch 用）

- [ ] **Step 1: 失败测试**（fake client 注入，不发真实网络）

```python
# backend/tests/test_intent_typesafe.py
from types import SimpleNamespace
import porto_chatbot.intent as intent_mod

class FakeTypesafe:
    def __init__(self, choice, confidence):
        self._r = SimpleNamespace(choices={
            "intent": SimpleNamespace(choice=choice,
                probabilities={}, confidence=confidence)})
    def system_one(self, *, state, questions, **kw):
        return self._r

def _settings(**kw):  # 审计修正：去掉 if-False 死代码，直接构造
    from porto_chatbot.settings import Settings
    return Settings(chat_typesafe_api_key="k-test",
                    chat_typesafe_confidence_threshold=0.6, **kw)

def test_typesafe_high_confidence_wins(monkeypatch):
    monkeypatch.setattr(intent_mod, "_build_typesafe_client",
                        lambda s: FakeTypesafe("direct", 0.93))
    d = intent_mod.route_chat_intent("在吗", _settings(), llm=None, engine="typesafe")
    assert d.intent == ChatIntent.DIRECT and d.engine == "typesafe"
    assert d.confidence == 0.93 and d.fallback_from is None

def test_typesafe_low_confidence_falls_back_to_builtin(monkeypatch):
    monkeypatch.setattr(intent_mod, "_build_typesafe_client",
                        lambda s: FakeTypesafe("direct", 0.2))
    d = intent_mod.route_chat_intent("帮我分析支付架构", _settings(), llm=None,
                                     engine="typesafe")
    assert d.fallback_from == "typesafe"
    assert d.reason.startswith("typesafe:low_confidence")

def test_typesafe_missing_key_degrades(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(intent_mod, "_build_typesafe_client",
                        lambda s: called.update(n=called["n"] + 1) or (_ for _ in ()).throw(AssertionError()))
    d = intent_mod.route_chat_intent("你好", _settings(chat_typesafe_api_key=None),
                                     llm=None, engine="typesafe")
    assert d.fallback_from == "typesafe" and called["n"] == 0

def test_typesafe_error_degrades(monkeypatch):
    def boom(s): raise RuntimeError("network down")
    monkeypatch.setattr(intent_mod, "_build_typesafe_client", boom)
    d = intent_mod.route_chat_intent("查支付文档", _settings(), llm=None, engine="typesafe")
    assert d.fallback_from == "typesafe"
```

- [ ] **Step 2: 确认失败**（`_build_typesafe_client` 不存在）

- [ ] **Step 3: 实现**（要点）

```python
def _build_typesafe_client(settings: Settings):
    from typesafe_sdk import TypeSafeClient  # lazy import
    kwargs = {}
    if settings.chat_typesafe_base_url:
        kwargs["base_url"] = settings.chat_typesafe_base_url
    return TypeSafeClient(api_key=settings.chat_typesafe_api_key, **kwargs)

def _typesafe_route(message, settings, llm, routing_mode) -> IntentDecision:
    if not message.strip():
        return _builtin_route(message, llm, routing_mode)  # 提取的原 llm+rule 流程
    if not settings or not settings.chat_typesafe_api_key:
        d = _builtin_route(...)
        return replace(d, fallback_from="typesafe",
                       reason=f"typesafe:fallback:not_configured|{d.reason}")
    try:
        client = _build_typesafe_client(settings)
        enums = ["direct", "quick_rag", "deep_rag"] if routing_mode == ADAPTIVE else ["direct", "rag"]
        resp = client.system_one(
            state={"message": message[:_MAX_INTENT_MESSAGE_CHARS]},
            questions={"intent": Choice(
                instructions=_TYPESAFE_INSTRUCTIONS,  # 中文分类标准，与 _llm_route desc 同源
                criteria={name: None for name in enums})},
            # 审计修正：真实 SDK 无 timeout_ms；timeout 单位是秒（float）。
            # RetryPolicy(max_retries=0)：路由预算 2s 内不允许 429/5xx 重试放大延迟。
            timeout=settings.chat_typesafe_timeout_ms / 1000,
            retry=RetryPolicy(max_retries=0),
        )
        ans = resp.choices["intent"]
        conf = float(getattr(ans, "confidence", 0.0) or 0.0)
        choice = str(ans.choice)
        if choice not in enums:
            raise ValueError(f"unexpected choice {choice}")
        if (conf < settings.chat_typesafe_confidence_threshold
                and settings.chat_typesafe_low_confidence_fallback):
            d = _builtin_route(message, llm, routing_mode)
            return replace(d, fallback_from="typesafe",
                           reason=f"typesafe:low_confidence:{conf:.2f}|{d.reason}")
        mapping = {e: ChatIntent(e) for e in enums}
        return IntentDecision(mapping[choice], f"typesafe:{choice}", confidence=conf, engine="typesafe")
    except Exception as exc:
        logger.warning("typesafe route failed, degrading: %s", exc)
        d = _builtin_route(message, llm, routing_mode)
        return replace(d, fallback_from="typesafe", reason=f"typesafe:fallback:error|{d.reason}")
```

安装 typesafe-sdk 后先在 REPL 核对 `TypeSafeClient` 真实签名（`typesafe_sdk` 包；超时/Choice 参数名以 `help()` 为准），再按实参调整——**以真实 SDK 签名为准，测试的 FakeTypesafe 不受影响**。`route_chat_intent` 主流程插入 `engine == TYPESAFE` 分支。

- [ ] **Step 4: 通过 + 全量回归**

- [ ] **Step 5: Commit** `feat(intent): typesafe decision engine with confidence gate and fallback`

---

### Task 5: 编排接线（orchestrator/stream/steps 元数据）

**Files:**
- Modify: `backend/src/porto_chatbot/agent/orchestrator.py:86-91`（`_route_intent` 传 engine）
- Modify: `backend/src/porto_chatbot/agent/langchain_chat_stream.py:350` 附近（同）
- Modify: 同两文件 route_intent step 的 `data`（加 `engine`/`confidence`/`fallback`）
- Test: `backend/tests/test_chat_dispatch.py`（追加）

**Interfaces:**
- Consumes: Task 3/4 的 `route_chat_intent(..., engine=)`
- Produces: steps 内 `{"name": "route_intent", "data": {"intent", "reason", "engine", "confidence", "fallback"}}`

- [ ] **Step 1: 失败测试**（审计修正：orch fixture 在 `backend/tests/agent/test_orchestrator.py:15-20`，非 test_chat_dispatch；patch 目标是 `porto_chatbot.agent.orchestrator.effective_rag_chat_settings`——orchestrator 模块内导入名）：追加到 `tests/agent/test_orchestrator.py`，复用其 `orch` fixture，monkeypatch 上述模块属性返回含 `intent_engine=rule` 的 payload，调用 `handle()`，断言 `response.steps[0].data["engine"] == "rule"`
- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现**：`_route_intent` 读 `rag_chat.intent_engine` 传入（唯一调用点 orchestrator.py:91，stream 经 `orch._route_intent` 自动生效）；`_handle_direct`/`_handle_rag` 的 step data 与 `_stream_rag_path`（langchain_chat_stream.py:261-267）加三字段；`_handle_rag_unavailable` 的 route step 同步补齐；读取用 `getattr(decision, "confidence", None)`（现有测试用 MagicMock patch `_route_intent`，直接属性访问会写入 Mock 对象）
- [ ] **Step 4: 通过 + 全量**
- [ ] **Step 5: Commit** `feat(chat): surface intent engine metadata in chat steps`

---

### Task 6: POST /api/intent/test 端点

**Files:**
- Create: `backend/src/porto_chatbot/api/routes/intent.py`
- Modify: `backend/src/porto_chatbot/api/app.py`（挂 router）
- Create: `backend/src/porto_chatbot/models/intent_test.py` 或并入 `models/payload.py`（`IntentTestRequest/Response`）
- Test: `backend/tests/test_intent_api.py`（新建）

**Interfaces:**
- Produces: `POST /api/intent/test`，请求 `{"message": str, "routing_mode"?: str, "engine"?: str}`，响应 `{"intent","reason","engine","confidence","fallback","latency_ms"}`；无副作用

- [ ] **Step 1: 失败测试**

```python
def test_intent_test_rule_engine(client):  # 审计修正：client fixture 是 test_settings_api.py 文件局部，新文件复制其 2 行模式
    r = client.post("/api/intent/test",
                    json={"message": "你好", "engine": "rule", "routing_mode": "binary"})
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] == "direct" and body["engine"] == "rule"
    assert body["latency_ms"] >= 0 and body["fallback"] is False

def test_intent_test_defaults_to_effective_settings(client):
    r = client.post("/api/intent/test", json={"message": "查一下支付文档"})
    assert r.status_code == 200 and r.json()["intent"] in ("rag", "quick_rag", "deep_rag", "direct")
```

- [ ] **Step 2: 确认失败**（404）
- [ ] **Step 3: 实现**：route 内 `time.monotonic()` 计时；engine/mode 缺省取 `effective_rag_chat_settings()`；`LLMClient(current_settings())` 传入；响应模型含全部字段
- [ ] **Step 4: 通过 + 全量**
- [ ] **Step 5: Commit** `feat(api): POST /api/intent/test decision probe endpoint`

---

### Task 7: 前端类型与默认值

**Files:**
- Modify: `frontend/src/lib/types.ts:116-127`（`RagChatConfig`）
- Modify: `frontend/src/lib/api.ts:246-318`（`defaultRagChatConfig` 镜像后端默认）

**Interfaces:**
- Produces:

```ts
export type IntentEngine = "builtin_llm" | "rule" | "typesafe";
// RagChatConfig 增：
intent_engine: IntentEngine;
chat_typesafe_api_key: string;
chat_typesafe_base_url: string;
chat_typesafe_timeout_ms: number;          // 2000
chat_typesafe_confidence_threshold: number; // 0.6
chat_typesafe_low_confidence_fallback: boolean; // true
```

- [ ] **Step 1: 加类型 + 默认值**（defaultRagChatConfig 同步六键默认；**审计修正：现有 `condense_enabled: true` 在 defaultRagChatConfig 中缺失，`tsc --noEmit` 当前即报错，本任务顺带补上**）
- [ ] **Step 2: 验收改为 `npx tsc --noEmit` 通过（修复存量报错）且改动行无新增 eslint 发现**（审计修正：repo 级 `npm run lint` 现存 1100 错误，不可能全绿）
- [ ] **Step 3: Commit** `feat(web): typesafe intent engine types and defaults`

---

### Task 8: Sidebar 分组重组

**Files:**
- Modify: `frontend/src/components/porto-workbench.tsx:106-113`（`SettingsSection` union 增 `"orchestration"`）、`:1445-1510`（sidebar 数组 → 分组渲染）

**Interfaces:**
- Produces: 4 组结构（对话引擎[agent] / 编排[orchestration] / 知识检索[rag, retrieval_optimization, vector_database] / 运行环境[document, knowledge, architecture]）；组头为不可点击小标题（`text-[11px] uppercase tracking-wide text-zinc-400 px-3 pt-4 pb-1`）；取消 child 缩进

- [ ] **Step 1: 实现**：sidebar 数据结构改为（审计修正：现有 icon 是 ReactNode（`<Gauge size={15}/>`），沿用该形态免类型改动）

```ts
const SETTINGS_GROUPS: { label: string; items: { id: SettingsSection; label: string; icon: LucideIcon }[] }[] = [
  { label: "对话引擎", items: [{ id: "agent", label: "Agent", icon: Bot }] },
  { label: "编排", items: [{ id: "orchestration", label: "编排", icon: Route }] },
  { label: "知识检索", items: [
      { id: "rag", label: "RAG", icon: Gauge },
      { id: "retrieval_optimization", label: "检索优化", icon: Search },
      { id: "vector_database", label: "向量数据库", icon: Database }] },
  { label: "运行环境", items: [
      { id: "document", label: "文件解析", icon: FileInput },
      { id: "knowledge", label: "Knowledge", icon: Database },
      { id: "architecture", label: "架构", icon: Network }] },
];
```

渲染双循环；选中态样式不变。`lucide-react` 的 `Route` 图标导入。

- [ ] **Step 2: 手动验证**（`make frontend-dev` + 打开设置页）：7 项全可达、orchestration 选中不渲染内容（下一任务补）
- [ ] **Step 3: Commit** `feat(web): grouped settings sidebar with orchestration section`

---

### Task 9: OrchestrationPage（总览 + 5 卡 + 锁定态 + 表单迁移）

**Files:**
- Modify: `frontend/src/components/porto-workbench.tsx`：
  - `SettingsSection` 渲染链（`:1526-1713`）加 orchestration 分支
  - 新增组件（置于 `RagOptimizationSettingsForm` 之后）：`PipelineOverviewRail`、`OrchestrationPage`、`TypesafePanel`
  - `RagOptimizationSettingsForm`（`:2664-3014`）：移除 chat 场景卡与 rerank 卡（迁入编排）
  - `AgentSettingsForm`（`:2102-2546`）：移除 memory_compact_threshold / memory_recent_keep / context_char_budget 三字段（迁入编排卡 5）
  - `PortoWorkbench`：新增编排保存 handlers（`saveOrchestrationRagChat` / `...Rag` / `...Agent` 局部键）

**Interfaces:**
- Consumes: Task 7 类型；现有 `saveAppSettings`（PUT 部分 namespace）；`POST /api/intent/test`
- Produces: 编排页完整交互（spec §4）

实现要点（关键代码结构）：

- **锁定判定与横幅**（页面顶部，唯一可交互元素为链接按钮）：

```tsx
const locked = agentConfig.chatbot_backend === "agent_sdk";
{locked && (
  <div role="note" className="mb-4 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-700" data-testid="orchestration-locked-banner">
    <p className="font-semibold">编排仅适用于 Langchain 聊天管道</p>
    <p className="mt-1 text-xs">当前 Chatbot 引擎为 Claude Agent SDK，此页配置暂不生效，已保存的配置会被完整保留。</p>
    <button className="mt-2 text-xs font-medium underline" onClick={() => setSection("agent")}>前往 Agent 设置 →</button>
  </div>
)}
<div aria-disabled={locked} className={locked ? "pointer-events-none opacity-50" : ""}>…卡片群…</div>
```

容器以 `key={agentConfig.chatbot_backend}` 重挂载（切引擎重建 draft）。

- **PipelineOverviewRail**：6 节点 `{id, label, summary}`（summary 从各 draft 计算，如 `Binary`/`HyDE`/`Hybrid·0.6`/`关闭`/`压缩 20`/`16000`），`data-testid="pipeline-overview"`；点击 `scrollIntoView({behavior:"smooth"})` + 目标卡 `ring-2 ring-zinc-950` 800ms；md+ 横向 `flex-row`、窄屏 `flex-col`（左侧竖线）；脏点：对应 draft ≠ saved 时节点右上 `bg-rose-500` 4px 圆点。

- **卡 1 意图路由**（`data-testid="stage-intent"`，rag_chat namespace 整卡保存）：
  - 分区 1 路由模式：复用 `StrategyCardGroup` + 现有 `ROUTING_OPTIONS`
  - 分区 2 路由引擎：三列卡片组（内置 LLM/规则引擎/TypeSafe 外部引擎，复用 EngineCardGroup 样式改 `sm:grid-cols-3`），`mode==="off"` 时禁用 + 提示
  - 分区 3 `<TypesafePanel draft onChange errors />`：`intent_engine==="typesafe" && mode!=="off"` 时展开；API Key password input + Eye/EyeOff；Base URL（placeholder「留空使用 TypeSafe 默认地址」）；超时 number + `ms` 后缀；阈值 slider(0–1, 0.05)+number 联动（两端标注「0 宽松 / 1 严格」）；低置信回退 checkbox + 说明；校验：typesafe 选中且 api_key 为空时点保存 → 字段 `border-rose-300` + 文案「使用 TypeSafe 引擎需要填写 API Key」（保存按钮不禁用）
  - 「发送测试消息」：输入框 + 按钮 → `POST /api/intent/test` → 展示 `intent / engine / confidence / fallback / latency_ms`（`data-testid="intent-test-result"`）
  - **审计修正（两个语义/接线点）**：① 空白 API Key 保存 = **省略该键**（payload 中不传/传 undefined），不是传 `""`——传空串会真实清空已存 key；② `setSection` 是 `SettingsPage` 内局部 state，横幅的「前往 Agent 设置」需把 `setSection` 作为 prop 传入 `OrchestrationPage`
  - **审计修正（新建 vs 迁移）**：`condense_enabled` 开关与 `hyde_fallback_threshold` 输入在现有 UI 中不存在，属新建控件；chat 场景卡与 rerank 卡是真迁移
- **卡 2 检索前变换**：chat 的 `query_transform_strategy` + 条件展开的 `multi_query_count`/`hyde_fallback_threshold` + `condense_enabled` checkbox（自 RagOptimizationSettingsForm 迁移，rag_chat namespace）
- **卡 3 检索**：`retrieval_method` 三选 + method==="hybrid" 时 `hybrid_vector_weight` slider（rag namespace 部分保存，PUT 只带这三键所在 payload 子集 `{retrieval_method, hybrid_vector_weight}`）
- **卡 4 重排**：`rerank_enabled` + 类型 llm/cross_encoder + top_n + provider/model/keys（现 RagOptimizationSettingsForm 的 rerank 卡整体迁入，rag namespace）
- **卡 5 记忆与上下文预算**：`memory_compact_threshold`/`memory_recent_keep`/`context_char_budget` number（agent namespace 部分保存，PUT 只带这三键）
- 每卡 `SettingsCard` + 序号 `01–05`（`font-mono text-xs text-zinc-400`）+ `id="stage-x"` 锚点 + `data-testid`
- 「检索优化」节剩 workflow 场景卡，标题改「Workflow 检索优化」

- [ ] **Step 1: 实现组件群与接线**
- [ ] **Step 2: 验收改为 `npx tsc --noEmit` 通过 + 手动走查**（langchain 模式配置/保存/刷新持久化；agent_sdk 模式锁定与恢复；审计修正：repo 级 lint 不可达全绿）
- [ ] **Step 3: Commit** `feat(web): orchestration settings page with pipeline overview and typesafe engine config`

---

### Task 10: Playwright E2E

**Files:**
- Create: `frontend/playwright.config.ts`、`frontend/e2e/orchestration.spec.ts`
- Modify: `frontend/package.json`（devDeps `@playwright/test` + `test:e2e` script）

**Interfaces:**
- Consumes: 双 dev server（backend :8100 `uv run uvicorn porto_chatbot.main:app`；frontend :3100 `PORTO_API_BASE_URL=http://127.0.0.1:8100 npm run dev`）；Makefile 端口常量
- Produces: `npx playwright test` 全绿

- [ ] **Step 1: 脚手架**

```ts
// frontend/playwright.config.ts
import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./e2e", timeout: 30_000, retries: 0,
  use: { baseURL: "http://127.0.0.1:3100" },
  webServer: [
    { command: "cd ../backend && uv run uvicorn porto_chatbot.main:app --port 8100",
      url: "http://127.0.0.1:8100/api/health", reuseExistingServer: true, timeout: 120_000,
      env: { PORTO_CHATBOT_DATA_DIR: "<e2e 临时目录>" } },  // 审计修正：隔离数据目录，避免污染 ~/.porto 真实配置
    { command: "npm run dev -- -p 3100",
      url: "http://127.0.0.1:3100", reuseExistingServer: true, timeout: 120_000,
      env: { PORTO_API_BASE_URL: "http://127.0.0.1:8100" } },
  ],
});
```

`npm i -D @playwright/test && npx playwright install chromium`。

- [ ] **Step 2: 用例**（`e2e/orchestration.spec.ts`，关键选择器用 data-testid）

1. `sidebar shows grouped sections and navigates to orchestration`：断言 4 组头 + 点编排 → `[data-testid="pipeline-overview"]` 可见
2. `intent engine persists after save and reload`：选「规则引擎」→ 保存 → `page.reload()` → 规则卡仍选中
3. `typesafe panel expands and validates empty api key`：选 TypeSafe → 面板可见；直接保存 → 字段错误文案可见
4. `intent test button returns rule decision`：路由模式 binary + 规则引擎，测试消息「你好」→ 结果含 `direct`
5. `orchestration locks under agent_sdk and recovers`：Agent 节切 Claude Agent SDK → 保存 → 编排页横幅可见 + 控件禁用；切回 Langchain → 可编辑、值保留

- [ ] **Step 3: 运行**

```bash
cd frontend && npx playwright test
```

- [ ] **Step 4: Commit** `test(e2e): playwright suite for orchestration settings`

---

## Self-Review 记录

- Spec 覆盖：§5.1→Task 3/4/5；§5.2→Task 2；§5.3→Task 6；§5.4→Task 1；§5.5→Task 7/8/9；§5.6→Task 10；§3/§4（产品/UX）→Task 8/9。无缺口。
- 类型一致性：`IntentEngine`/`chat_typesafe_*` 命名在 Task 2/4/7 一致；`_build_typesafe_client` 在 Task 4 定义并供测试注入。
- 占位符：Task 4 Step 3 的「以真实 SDK 签名为准」为实现期核对项，非占位——FakeTypesafe 已锁定行为契约。
