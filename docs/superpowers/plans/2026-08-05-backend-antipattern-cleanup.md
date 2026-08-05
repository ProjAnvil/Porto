# 后端反模式消除 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除后端 Python 代码中的 4 类反模式：dict 返回值、静默 except、魔法数字、过长函数。

**Architecture:** 先建结构化类型（Pydantic model / dataclass）替换 dict 返回值，再修补静默 except 和魔法数字，最后拆分过长函数。

**Spec:** `docs/superpowers/specs/2026-08-05-backend-antipattern-cleanup-design.md`

## Global Constraints

- Python 3.12+，Pydantic v2
- evaluation model 赋值给 `dict[str, Any]` 字段时必须显式 `.model_dump()`
- LangGraph state 中存储的 evaluation 可能被序列化为 dict — evaluate 节点收到的可能是 dict 或 model，需用 `getattr()` 安全访问
- 函数拆分不改变公共签名和运行时行为
- 测试环境：`cd backend && .venv/bin/python -m pytest`
- 每完成一个 Task 运行相关测试 + ruff check，PASS 后 commit

---

### Task 1: 新建 evaluation models + 更新 evaluation.py

**Files:**
- Create: `backend/src/porto_chatbot/models/evaluation.py`
- Modify: `backend/src/porto_chatbot/evaluation.py`
- Modify: `backend/src/porto_chatbot/models/__init__.py`

- [ ] **Step 1: 创建 models/evaluation.py**

定义 5 个 Pydantic model（spec §2.1）。注意 `WorkflowEvaluation` 需包含 `spec_rubric_avg: float | None = None` 和 `spec_rubric_min: float | None = None`。

- [ ] **Step 2: 更新 models/__init__.py**

从 `.evaluation` 导入并导出 5 个 model。

- [ ] **Step 3: 更新 evaluation.py**

- `from .models.evaluation import WorkflowCheck, WorkflowEvaluation, RagMetrics, RagCaseEvaluation, RagBatchEvaluation`
- `evaluate_workflow() -> dict` → `-> WorkflowEvaluation`
  - `checks` 列表中 dict → `WorkflowCheck(name=..., passed=..., weight=...)`
  - `c["weight"]`、`c["passed"]` → `c.weight`、`c.passed`
  - `result = {"score": ..., "passed": ..., "checks": ..., "prd_chars": ...}` → `WorkflowEvaluation(score=..., passed=..., checks=..., prd_chars=...)`
  - `result["score"]`、`result["passed"]` → `result.score`、`result.passed`
- `evaluate_rag_case() -> dict` → `-> RagCaseEvaluation`
  - `metrics` dict → `RagMetrics(...)`
  - `result` dict → `RagCaseEvaluation(...)`
- `evaluate_rag_cases() -> dict` → `-> RagBatchEvaluation`
  - `r["score"]` → `r.score`
  - `result` dict → `RagBatchEvaluation(...)`

- [ ] **Step 4: 测试 + 测试适配**

```bash
cd backend && .venv/bin/python -m pytest tests/test_evaluation.py tests/test_memory_eval.py -q
```
`test_evaluation.py` 和 `test_memory_eval.py` 中的 dict 访问（`result["score"]`、`result.cases[0]["metrics"]["groundedness"]` 等）需改为属性访问（`result.score`、`result.cases[0].metrics.groundedness`）。

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/models/evaluation.py backend/src/porto_chatbot/models/__init__.py backend/src/porto_chatbot/evaluation.py backend/tests/test_evaluation.py
git commit -m "refactor(evaluation): replace dict returns with Pydantic models"
```

---

### Task 2: SessionSummary dataclass + compaction 消费适配

**Files:**
- Modify: `backend/src/porto_chatbot/memory/store.py`
- Modify: `backend/src/porto_chatbot/memory/compaction.py`

- [ ] **Step 1: 更新 memory/store.py**

- 添加 `from dataclasses import dataclass`
- 新增 dataclass `SessionSummary(summary: str, last_message_id: str, created_at: str)`
- `get_summary() -> dict | None` → `-> SessionSummary | None`
- 函数内部 `return {"summary": row[0], ...}` → `return SessionSummary(summary=row[0], last_message_id=row[1], created_at=row[2])`

- [ ] **Step 2: 更新 memory/compaction.py**

找到 `get_summary()` 调用点（约行 61-67）：
- `cached.get("last_message_id")` → `cached.last_message_id`
- `cached.get("summary")` → `cached.summary`
- `cached["summary"]` → `cached.summary`

- [ ] **Step 3: 测试**

```bash
cd backend && .venv/bin/python -m pytest tests/memory/test_compaction.py tests/memory/ -q
```

- [ ] **Step 4: Commit**

```bash
git add backend/src/porto_chatbot/memory/store.py backend/src/porto_chatbot/memory/compaction.py
git commit -m "refactor(memory): replace get_summary dict return with SessionSummary dataclass"
```

---

### Task 3: 静默 except 补日志

**Files:**
- Modify: `backend/src/porto_chatbot/api/deps.py`
- Modify: `backend/src/porto_chatbot/workflow_executor.py`

- [ ] **Step 1: 更新 api/deps.py（3 处）**

找到 `reset_rag_singletons` 函数中的 3 处 `except Exception:`：
- 行 ~298: `except Exception:` → `except Exception as exc:` + `logger.warning("supervisor stop failed: %s", exc)`
- 行 ~302: `except Exception:` → `except Exception as exc:` + `logger.warning("health stop failed: %s", exc)`
- 行 ~318: `except Exception:` → `except Exception as exc:` + `logger.warning("checkpoint conn close failed: %s", exc)`

注意：需要确认 `logger` 在 `api/deps.py` 中已导入/定义。如果没有，添加 `from ..logging_utils import get_component_logger; logger = get_component_logger("deps")`。

- [ ] **Step 2: 更新 workflow_executor.py（2 处）**

- 行 ~151: `except Exception:` → `except Exception as exc:` + `logger.warning("update_status FAILED failed: %s", exc)`
- 行 ~402: 同上

- [ ] **Step 3: 测试**

```bash
cd backend && .venv/bin/python -m pytest tests/test_workflow_executor.py tests/test_workflow_startup_recovery.py -q
```

- [ ] **Step 4: Commit**

```bash
git add backend/src/porto_chatbot/api/deps.py backend/src/porto_chatbot/workflow_executor.py
git commit -m "refactor: add logging to 5 silent except Exception blocks"
```

---

### Task 4: 魔法数字提取为常量

**Files:**
- Modify: `backend/src/porto_chatbot/intent.py`
- Modify: `backend/src/porto_chatbot/llm/client.py`
- Modify: `backend/src/porto_chatbot/vector_store.py`
- Modify: `backend/src/porto_chatbot/specs/loop.py`
- Modify: `backend/src/porto_chatbot/agent/heuristics.py`
- Modify: `backend/src/porto_chatbot/agent/langchain_chat.py`
- Modify: `backend/src/porto_chatbot/memory/store.py`
- Modify: `backend/src/porto_chatbot/evaluation.py`

- [ ] **Step 1: 提取常量**

按 spec §4 的清单，在每个文件顶部（import 之后）添加命名常量，替换硬编码值：

- `intent.py`: `_MAX_INTENT_MESSAGE_CHARS = 500`, `_MAX_REASON_CHARS = 80`, `_SHORT_MESSAGE_THRESHOLD = 12`
- `llm/client.py`: `_CONTINUATION_TAIL_CHARS = 200`
- `vector_store.py`: `_CHUNK_ID_PREVIEW_LEN = 120`, `_EMBED_BATCH_SIZE = 64`
- `specs/loop.py`: `_FEEDBACK_DIGEST_MAX = 200`
- `agent/heuristics.py`: `_MAX_PROJECT_NAME_CHARS = 40`, `_SUMMARY_MAX_CHARS = 180`, `_MAX_LIST_ITEMS = 12`
- `agent/langchain_chat.py`: `_SOURCE_PREVIEW_CHARS = 180`, `_MAX_FALLBACK_SOURCES = 4`, `_MAX_SSE_SOURCES = 6`
- `memory/store.py`: `_DEFAULT_MESSAGE_FETCH_LIMIT = 500`
- `evaluation.py`: `_MIN_UNDERSTANDING_CHARS = 120`, `_RAG_PASS_SCORE = 55`, `_SENTENCE_SUPPORT_THRESHOLD = 0.35`

- [ ] **Step 2: 替换硬编码值**

在每个文件中用常量替换对应的硬编码数字。

- [ ] **Step 3: 测试 + lint**

```bash
cd backend && .venv/bin/python -m pytest tests/test_intent.py tests/test_evaluation.py tests/test_vector_store.py tests/test_spec_loop.py tests/test_llm_client_truncation.py -q
cd backend && .venv/bin/python -m ruff check src/
```

- [ ] **Step 4: Commit**

```bash
git add backend/src/porto_chatbot/
git commit -m "refactor: extract magic numbers into named constants"
```

---

### Task 5: evaluation model 消费链适配

**Files:**
- Modify: `backend/src/porto_chatbot/agent/langchain_chat.py`
- Modify: `backend/src/porto_chatbot/agent_sdk/backend.py`
- Modify: `backend/src/porto_chatbot/agent/nodes/evaluate.py`
- Modify: `backend/src/porto_chatbot/workflow_executor.py`
- Modify: `backend/src/porto_chatbot/api/routes/eval.py`（如有）

- [ ] **Step 1: 在 evaluate_rag_cases / evaluate_workflow 调用点立即 .model_dump()**

**核心策略**：在所有调用 `evaluate_rag_cases(...)` 或 `evaluate_workflow(...)` 的位置，紧跟 `.model_dump()` 将 model 立即转为 dict。这样下游所有 `evaluation['score']`、SSE 嵌入点、ChatResponse 赋值都不需要改——evaluation 变量从一开始就是 dict。

具体调用点（用 grep `evaluate_rag_cases\|evaluate_workflow` 定位）：
- `agent/langchain_chat.py`: `evaluate_rag_cases(...)` → `evaluate_rag_cases(...).model_dump()`
- `agent_sdk/backend.py`: 同上（注意三元表达式 `evaluate_rag_cases([...]) if sources else {...}` → model 分支加 `.model_dump()`）
- `workflow_executor.py`: `evaluate_workflow(...)` → `evaluate_workflow(...).model_dump()`（如投影路径中有调用）
- `api/routes/eval.py`: `return evaluate_rag_cases(...)` → `return evaluate_rag_cases(...).model_dump()`（保持 API JSON 一致）

- [ ] **Step 2: 适配 agent/nodes/evaluate.py（关键！None 陷阱）**

evaluate 节点直接调用 `evaluate_workflow()` 获取 `WorkflowEvaluation` model。需要：
1. `evaluation = evaluate_workflow(...)` 保持返回 model（不 .model_dump()，因为节点需要用结构化属性）
2. **所有 dict 操作改为属性操作**：
   - `evaluation["spec_rubric_avg"] = round(...)` → `evaluation = evaluation.model_copy(update={"spec_rubric_avg": round(...)})`（Pydantic v2 不可变更新）
   - `evaluation["spec_rubric_min"] = min(...)` → 同上
   - `evaluation.get("passed", True)` → `evaluation.passed`
   - `evaluation.get("score")` → `evaluation.score`
   - `evaluation.get("spec_rubric_avg")` → `evaluation.spec_rubric_avg`
   - `evaluation['score']` → `evaluation.score`
3. **None 陷阱处理**：`evaluation.get("spec_rubric_min", default)` 在 dict 时代 key 不存在返回 default；model 时代字段始终存在（默认 None）。必须显式检查：
   ```python
   rubric_min = evaluation.spec_rubric_min
   if rubric_min is None:
       rubric_min = agent.settings.spec_refine_pass_score
   below_bar = (not evaluation.passed) or (rubric_min < agent.settings.spec_refine_pass_score)
   ```
4. 节点返回 `{"evaluation": evaluation.model_dump()}`（转 dict 进入 LangGraph state，避免 checkpoint serde 问题）

- [ ] **Step 3: checkpoint serde 注册（可选但推荐）**

`api/deps.py` 的 `_build_checkpoint_serde` 中 `allowed_msgpack_modules` 添加 WorkflowEvaluation。但如果 Step 2 已在节点返回前 `.model_dump()`，则 checkpoint 中存储的是 dict，不需要注册。

- [ ] **Step 4: 适配 test_agent.py**

`tests/test_agent.py` 中 `result["evaluation"]["spec_rubric_avg"]` 等 dict 访问，改为 `result["evaluation"]["spec_rubric_avg"]`（保持 dict 访问——因为 evaluate 节点返回前已 `.model_dump()`，state 中是 dict）。

- [ ] **Step 5: 测试**

```bash
cd backend && .venv/bin/python -m pytest tests/test_agent.py tests/test_agent_graph.py tests/test_chat_dispatch.py tests/test_agent_sdk_chat.py tests/test_workflow_api.py tests/test_workflow_executor.py tests/api/ -q
```

- [ ] **Step 4: Commit**

```bash
git add backend/src/porto_chatbot/
git commit -m "refactor: adapt evaluation model consumers with .model_dump()"
```

---

### Task 6: 拆分 langchain_chat_stream（212 行）

**Files:**
- Modify: `backend/src/porto_chatbot/agent/langchain_chat.py`

- [ ] **Step 1: 分析 langchain_chat_stream 结构**

读取函数完整内容（行 277-488），识别可提取的逻辑块：
- 意图路由
- 寒暄/直接回复路径（SSE 编码）
- RAG 检索路径（检索 + facts + 流式回复）
- 结果组装

- [ ] **Step 2: 提取子函数**

按 spec §5.1 策略提取：
- `_handle_stream_direct(req, settings)` — 流式寒暄路径
- `_handle_stream_rag(req, settings)` — 流式 RAG 路径
- `langchain_chat_stream` 缩减为路由分发

保持函数签名不变。子函数返回中间结果，由主函数组装最终 SSE 响应。

- [ ] **Step 3: 测试**

```bash
cd backend && .venv/bin/python -m pytest tests/test_chat_dispatch.py tests/test_agent.py tests/test_langgraph_orchestration_spike.py -q
```

- [ ] **Step 4: Commit**

```bash
git add backend/src/porto_chatbot/agent/langchain_chat.py
git commit -m "refactor(langchain_chat): split langchain_chat_stream into focused subfunctions"
```

---

### Task 7: 拆分 agent_sdk chat / chat_stream

**Files:**
- Modify: `backend/src/porto_chatbot/agent_sdk/backend.py`

- [ ] **Step 1: 分析 chat 和 chat_stream 结构**

读取 `chat`（行 388+，~115 行）和 `chat_stream`（行 505+，~162 行）完整内容。

- [ ] **Step 2: 拆分 chat_stream**

提取：
- `_consume_stream_events(...)` — event 循环处理
- `_assemble_stream_response(...)` — 结果组装
- `chat_stream` 缩减为会话初始化 + 调用

- [ ] **Step 3: 拆分 chat**

提取：
- `_consume_chat_events(...)` — message 循环处理
- `chat` 缩减为初始化 + 调用

- [ ] **Step 4: 测试**

```bash
cd backend && .venv/bin/python -m pytest tests/test_agent_sdk_backend.py tests/test_agent_sdk_chat.py tests/test_chat_dispatch.py -q
```

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/agent_sdk/backend.py
git commit -m "refactor(agent_sdk): split chat and chat_stream into focused methods"
```

---

### Task 8: 全量测试回归 + Lint

- [ ] **Step 1: 全量测试**

```bash
cd backend && .venv/bin/python -m pytest -q
```
Expected: 全部 PASS

- [ ] **Step 2: Lint**

```bash
cd backend && .venv/bin/python -m ruff check src/
```
Expected: 无错误

- [ ] **Step 3: 最终 commit（如有修复）**

```bash
git add -A
git commit -m "refactor: final cleanup for anti-pattern elimination"
```
