# LangChain Client Migration (L1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `LLMClient` 底层从原生 `anthropic`/`openai` SDK 替换为 langchain `BaseChatModel`,对外 6 个方法 + 2 个属性签名完全不变;并跑通 4 个 langgraph/langchain 行为 spike 为 L2/L3 扫清未决项。

**Architecture:** `LLMClient._client` 从原生 SDK 实例换成 `ChatOpenAI`/`ChatAnthropic`;`complete`/`stream` 走 `invoke`/`stream`,`complete_with_tools` 走 `bind_tools` 循环,`complete_structured` 保留"prompt 注入 schema + JSON 解析重试"土办法,`complete_document` 依 spike 结果定 langchain 多模态或保留原生。检索层(llama-index)、settings、8 处调用方零改动。

**Tech Stack:** Python ≥3.12(实际 venv 3.14)、langchain-core、langchain-openai、langchain-anthropic、langgraph、pytest、ruff。

## Global Constraints

- **接口不变**:`LLMClient` 的 `complete` / `complete_structured` / `complete_with_tools` / `stream` / `complete_document` 方法签名与 [`llm/types.py`](../../backend/src/porto_chatbot/llm/types.py) 的 `ToolDef`/`ToolCall`/`ToolLoopResult`/`Message`/`ModelCapabilities` 类型**一字不改**。8 处调用方零改动。
- **降级保留**:`agent_api_key` 缺失时 `_client=None`、`enabled=False`,所有方法返回 `None`/空/`ToolLoopResult(text="")`。
- **配置零改动**:`settings.py` 的 `agent_provider`/`agent_api_key`/`agent_base_url`/`agent_model`/`agent_temperature`/`agent_max_tokens`/`agent_request_timeout` 字段与 env 别名(`LANGCHAIN_*`)不变。
- **测试构造 Settings(重要)**:`Settings` 字段带 `validation_alias` 且 `model_config` 未开 `populate_by_name`,直接 `Settings(agent_api_key="k")` 的 kwarg 会被**静默丢弃**(Task 2 发现)。测试里构造"带 key"的 settings 必须用 `Settings(kb_dirs=[...], data_dir=..., log_dir=...).model_copy(update={"agent_api_key": "k", "agent_provider": "openai", "agent_model": "..."})`。Task 5/11 的测试代码均按此模式,不要用裸 kwargs。
- **检索层不动**:llama-index 相关文件零改动;`ToolDef.handler` 仍调 llama-index。
- **tool calling 封装**:`complete_with_tools` 内部用 `bind_tools` 循环,不引入 graph 层 `ToolNode`。
- **structured output 用土办法**:不用 `with_structured_output`,保留 prompt 注入 JSON schema + `_try_parse_json` 重试。
- **代码风格**:ruff line-length 100,target py312;testpaths=["tests"]、pythonpath=["src"]。
- **Python 解释器**:backend venv 未默认激活,`python` 不在 PATH。**下文所有 `python` / `pytest` 命令一律指 `./.venv/bin/python`**(在 `backend/` 下执行),如 `./.venv/bin/python -m pytest tests/...`、`./.venv/bin/python -m ruff check src tests`。如已 `source backend/.venv/bin/activate` 则可直接用 `python`。
- **测试 PDF**:已由 `backend/scripts/make_test_pdf.py` 生成在 `backend/tests/fixtures/test_prd.pdf`(纯标准库构造,pypdf 可读)。
- **设计文档**:`docs/superpowers/specs/2026-07-24-langchain-langgraph-migration-design.md`(分支 `feat/langchain-langgraph-migration` 已提交)。

## Scope

本 plan 覆盖设计文档 **阶段 0(spike)+ 阶段 1(L1)**。L2(graph 编排)、L3(spec 子图)依赖 spike 结果,**各自另写 plan**。本 plan 结束时:底层已是 langchain,编排仍是手写 `WorkflowRunner`,全量测试绿,可独立 ship。

---

## File Structure

| 文件 | 职责 | 本 plan 动作 |
|---|---|---|
| `backend/pyproject.toml` | 依赖 | Modify(加 langchain/langgraph) |
| `backend/src/porto_chatbot/llm/client.py` | `LLMClient` 实现 | **重写内部**(_build_client/complete/stream/structured/with_tools/document) |
| `backend/src/porto_chatbot/llm/types.py` | `ToolDef` 等类型 | **不变** |
| `backend/src/porto_chatbot/llm/parsing.py` | `_try_parse_json` | 不变 |
| `backend/tests/test_llm_modern.py` | LLMClient 行为测试 | Modify(mock 基座换 `FakeChatModel`,断言不变) |
| `backend/tests/test_llm_timeout.py` | 超时测试 | Modify(适配 `BaseChatModel`) |
| `backend/tests/test_llm_langchain.py` | L1 新增(langchain 适配点) | Create |
| `backend/tests/test_langgraph_spike.py` | Send map-reduce + SqliteSaver 行为验证 | Create |
| `backend/scripts/spike_pdf_document.py` | PDF 原生输入 spike(手动) | Create |

---

## Tasks

### Task 1: 加 langchain / langgraph 依赖

**Files:**
- Modify: `backend/pyproject.toml`

**Interfaces:**
- Produces: 可 import 的 `langchain_openai.ChatOpenAI` / `langchain_anthropic.ChatAnthropic` / `langchain_core.language_models.BaseChatModel` / `langgraph.graph.StateGraph`

- [ ] **Step 1: 加依赖到 pyproject.toml**

在 `backend/pyproject.toml` 的 `dependencies` 列表(字母序)插入:

```toml
  "langchain-anthropic>=0.3.0",
  "langchain-core>=0.3.0",
  "langchain-openai>=0.3.0",
  "langchain-text-splitters>=1.0.0",
  "langgraph>=0.4.0",
```

(保留原有 `langchain-text-splitters`。`langchain-openai` 已附带 `langchain-core` 但显式声明更稳。)

- [ ] **Step 2: 安装**

Run: `cd backend && uv sync` (若项目用 uv) 或 `cd backend && pip install -e .`
Expected: 安装成功,无依赖冲突。

- [ ] **Step 3: 冒烟验证导入**

Run:
```bash
cd backend && python -c "from langchain_openai import ChatOpenAI; from langchain_anthropic import ChatAnthropic; from langchain_core.language_models import BaseChatModel; from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage; from langgraph.graph import StateGraph, START, END; from langgraph.types import Send; from langgraph.checkpoint.sqlite import SqliteSaver; print('ok')"
```
Expected: 输出 `ok`,无 ImportError。

- [ ] **Step 4: 提交**

```bash
git add backend/pyproject.toml backend/uv.lock
git commit -m "build(backend): 加 langchain / langgraph 依赖"
```

---

### Task 2: spike — ChatModel 构造 + PDF document(U4)

**目的**:验证 ① 无 key 时 disabled、有 key 时 `ChatOpenAI`/`ChatAnthropic` 能用现有 settings 构造(自动化);② provider 特定 PDF document block 在 langchain 多模态消息下是否可用(手动,结论决定 Task 10)。

**Files:**
- Create: `backend/tests/test_llm_langchain.py`(构造部分,永久保留)
- Create: `backend/scripts/spike_pdf_document.py`(手动 spike)

**Interfaces:**
- Produces: `_build_client` 应返回 `ChatOpenAI(agent_provider=="openai")` / `ChatAnthropic(agent_provider=="anthropic")` / `None`(无 key)。PDF 结论记录在本 plan 末尾 §Spike Conclusions。

- [ ] **Step 1: 写构造测试(此时 `_build_client` 还是旧实现,测试会 FAIL)**

`backend/tests/test_llm_langchain.py`:

```python
from __future__ import annotations

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from porto_chatbot.llm import LLMClient
from porto_chatbot.settings import Settings


def _settings(tmp_path, **over):
    base = dict(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        agent_api_key="k",
        agent_model="gpt-4.1-mini",
    )
    base.update(over)
    return Settings(**base)


def test_build_client_disabled_without_key(tmp_path):
    s = Settings(kb_dirs=[tmp_path / "kb"], data_dir=tmp_path / "data", log_dir=tmp_path / "logs")
    c = LLMClient(s)
    assert c.enabled is False
    assert c._client is None


def test_build_client_openai(tmp_path):
    c = LLMClient(_settings(tmp_path, agent_provider="openai"))
    assert c.enabled is True
    assert isinstance(c._client, ChatOpenAI)
    assert isinstance(c._client, BaseChatModel)


def test_build_client_anthropic(tmp_path):
    c = LLMClient(_settings(tmp_path, agent_provider="anthropic", agent_model="claude-sonnet-4-5"))
    assert c.enabled is True
    assert isinstance(c._client, ChatAnthropic)


def test_build_client_base_url_passed(tmp_path):
    c = LLMClient(_settings(tmp_path, agent_base_url="https://my.gateway/v1"))
    assert isinstance(c._client, ChatOpenAI)
    assert c._client.openai_api_base == "https://my.gateway/v1"
```

- [ ] **Step 2: 运行,确认 FAIL**

Run: `cd backend && python -m pytest tests/test_llm_langchain.py -v`
Expected: 3 个 `isinstance(..., ChatOpenAI/ChatAnthropic)` 用例 FAIL(旧 `_client` 是原生 `OpenAI`/`Anthropic` 实例)。`test_build_client_disabled_without_key` 可能已 PASS。

- [ ] **Step 3: 写 PDF spike 脚本**

`backend/scripts/spike_pdf_document.py`:

```python
"""Spike: 验证 ChatOpenAI/ChatAnthropic 能否处理 provider 特定 PDF document。

手动运行(需 LANGCHAIN_API_KEY + 一个 PDF 文件):
    cd backend && python -m scripts.spike_pdf_document <pdf> | openai|anthropic

结论填回本 plan §Spike Conclusions 与设计文档 §11 U4。
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from porto_chatbot.settings import settings


def main(pdf: str, provider: str) -> None:
    data = Path(pdf).read_bytes()
    encoded = base64.standard_b64encode(data).decode("ascii")
    prompt = "用中文摘要这份文档的核心需求。"
    model_name = settings.agent_model

    if provider == "openai":
        model = ChatOpenAI(model=model_name, api_key=settings.agent_api_key, base_url=settings.agent_base_url)
        msg = HumanMessage(content=[
            {"type": "file", "file": {"filename": Path(pdf).name, "file_data": f"data:application/pdf;base64,{encoded}"}},
            {"type": "text", "text": prompt},
        ])
    else:
        model = ChatAnthropic(model=model_name, api_key=settings.agent_api_key, base_url=settings.agent_base_url)
        msg = HumanMessage(content=[
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": encoded}},
            {"type": "text", "text": prompt},
        ])

    resp = model.invoke([msg])
    print(type(resp.content), resp.content if isinstance(resp.content, str) else "(multimodal content)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
```

- [ ] **Step 4: 手动跑 PDF spike 并记录结论**

Run(key 已在 `backend/.env` 配好;PDF 已生成):
```bash
./.venv/bin/python -m scripts.spike_pdf_document tests/fixtures/test_prd.pdf openai
./.venv/bin/python -m scripts.spike_pdf_document tests/fixtures/test_prd.pdf anthropic
```
把结果(成功/报错/内容形态)记到本 plan §Spike Conclusions 的 U4 行。**这个 Step 不阻塞后续**——Task 10 会给出 langchain 与保留原生两套实现,依此结论二选一。

- [ ] **Step 5: 提交(spike 脚本 + 失败的测试先入库,Task 5 再让测试转绿)**

```bash
git add backend/tests/test_llm_langchain.py backend/scripts/spike_pdf_document.py
git commit -m "test(llm): L1 构造测试 + PDF spike 脚本(红)"
```

---

### Task 3: spike — langgraph Send map-reduce 行为(U1/U2)

**目的**:验证 sync graph 下 `Send` 能把同一节点扇出到多个子图执行实例、并用 reducer 汇聚回父图 state;以及并发是并行还是串行(影响 L3)。结论记录到 §Spike Conclusions。

**Files:**
- Create: `backend/tests/test_langgraph_spike.py`

- [ ] **Step 1: 写 spike 测试**

`backend/tests/test_langgraph_spike.py`:

```python
from __future__ import annotations

from typing import Annotated, TypedDict

from langgraph.graph import START, END, StateGraph
from langgraph.types import Send


def _merge(left: dict, right: dict) -> dict:
    return {**(left or {}), **(right or {})}


class ParentState(TypedDict, total=False):
    items: list[str]
    results: Annotated[dict, _merge]


class SubState(TypedDict, total=False):
    item: str
    out: str


def _fanout(state: ParentState):
    return [Send("process", {"item": x}) for x in state["items"]]


def _process(state: SubState):
    return {"out": state["item"] + "_done"}


def _reduce_subgraph():
    sub = StateGraph(SubState)
    sub.add_node("process", _process)
    sub.add_edge(START, "process")
    sub.add_edge("process", END)
    return sub.compile()


def test_send_map_reduce_collects_all_items():
    parent = StateGraph(ParentState)
    parent.add_node("fanout", _fanout)
    parent.add_node("process", _reduce_subgraph())
    parent.add_edge(START, "fanout")
    parent.add_conditional_edges("fanout", lambda x: x)
    parent.add_edge("process", END)
    graph = parent.compile()

    result = graph.invoke({"items": ["a", "b", "c"]})
    assert set(result["results"].keys()) == {"a_done", "b_done", "c_done"}
```

- [ ] **Step 2: 运行**

Run: `cd backend && python -m pytest tests/test_langgraph_spike.py::test_send_map_reduce_collects_all_items -v`
Expected: PASS。**若 FAIL**(Send 到子图节点 + reducer 的行为不符),把实际行为记到 §Spike Conclusions U1,L3 改用"节点内 ThreadPool 跑子图"退化方案(设计文档 §11)。

- [ ] **Step 3: 提交**

```bash
git add backend/tests/test_langgraph_spike.py
git commit -m "test(spike): langgraph Send map-reduce 行为验证"
```

---

### Task 4: spike — SqliteSaver 多线程(U3)

**目的**:验证 `SqliteSaver` 在多个 daemon 线程(不同 thread_id)并发调用下不冲突(影响 L2)。

**Files:**
- Modify: `backend/tests/test_langgraph_spike.py`

- [ ] **Step 1: 追加多线程测试**

在 `backend/tests/test_langgraph_spike.py` 末尾追加:

```python
import sqlite3
import threading

from langgraph.checkpoint.sqlite import SqliteSaver


class _SState(TypedDict, total=False):
    value: int


def _inc(state: _SState):
    return {"value": state.get("value", 0) + 1}


def test_sqlite_saver_concurrent_threads(tmp_path):
    db = tmp_path / "cp.sqlite3"
    saver = SqliteSaver(sqlite3.connect(str(db), check_same_thread=False))
    g = StateGraph(_SState)
    g.add_node("inc", _inc)
    g.add_edge(START, "inc")
    g.add_edge("inc", END)
    graph = g.compile(checkpointer=saver)

    errors: list[BaseException] = []

    def run(tid: str):
        try:
            graph.invoke({"value": 0}, {"configurable": {"thread_id": tid}})
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(f"t{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent SqliteSaver errors: {errors}"
```

- [ ] **Step 2: 运行**

Run: `cd backend && python -m pytest tests/test_langgraph_spike.py::test_sqlite_saver_concurrent_threads -v`
Expected: PASS。**若 FAIL**(并发冲突),记到 §Spike Conclusions U3,L2 改用串行化层或 AsyncSqliteSaver。

- [ ] **Step 3: 提交**

```bash
git add backend/tests/test_langgraph_spike.py
git commit -m "test(spike): SqliteSaver 多线程并发验证"
```

---

### Task 5: `_build_client` 重写(原生 → ChatModel)

**Files:**
- Modify: `backend/src/porto_chatbot/llm/client.py:459-481`(`_build_client`)
- Test: `backend/tests/test_llm_langchain.py`(Task 2 已写)

**Interfaces:**
- Produces: `LLMClient._client: BaseChatModel | None`;`_build_client(self)` 返回 `ChatOpenAI`/`ChatAnthropic`/`None`。

- [ ] **Step 1: 修改 imports 与 `_build_client`**

在 `backend/src/porto_chatbot/llm/client.py` 顶部,**新增** langchain imports —— `from anthropic import Anthropic` / `from openai import OpenAI` **保留不删**(`complete_document` 走方案 B 仍需原生 SDK,见 §Spike Conclusions U4 / Task 10):

```python
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
```

把 `_build_client` 方法(约 459-481 行)整体替换为:

```python
    def _build_client(self) -> BaseChatModel | None:
        if not self.settings.agent_api_key:
            self.logger.info(
                "llm client disabled missing api key provider=%s", self.settings.agent_provider
            )
            return None
        kwargs: dict[str, Any] = {
            "api_key": self.settings.agent_api_key,
            "model": self.settings.agent_model,
            "temperature": self.settings.agent_temperature,
            "max_tokens": self.settings.agent_max_tokens,
            "timeout": self.settings.agent_request_timeout,
        }
        if self.settings.agent_base_url:
            kwargs["base_url"] = self.settings.agent_base_url
        if self.settings.agent_provider == "openai":
            client: BaseChatModel = ChatOpenAI(**kwargs)
        elif self.settings.agent_provider == "anthropic":
            client = ChatAnthropic(**kwargs)
        else:
            raise ValueError(f"Unsupported agent provider: {self.settings.agent_provider}")
        return client
```

`enabled` 属性(引用 `self._client is not None`)无需改动。

- [ ] **Step 2: 运行构造测试**

Run: `cd backend && python -m pytest tests/test_llm_langchain.py -v`
Expected: 4 个用例全 PASS。

- [ ] **Step 3: 此时其余 test_llm_modern.py 会红(FakeOpenAI 不再适配)——预期,Task 11 统一修**

Run: `cd backend && python -m pytest tests/test_llm_modern.py -v 2>&1 | tail -5`
Expected: 多个 FAIL(底层换了,旧 Fake mock 失效)。**这是预期的中间状态**,不在此 Task 修。

- [ ] **Step 4: 提交**

```bash
git add backend/src/porto_chatbot/llm/client.py
git commit -m "refactor(llm): _build_client 换 langchain ChatModel"
```

---

### Task 6: 消息转换 + `complete()` 重写

**Files:**
- Modify: `backend/src/porto_chatbot/llm/client.py`(`complete`、新增 `_to_lc_messages`、删除 `_openai_text`/`_anthropic_text`/`_split_system`)

**Interfaces:**
- Produces: `_to_lc_messages(self, msgs: list[Message]) -> list[BaseMessage]`;`complete` 走 `self._client.invoke(...)`。

- [ ] **Step 1: 写失败测试(追加到 test_llm_langchain.py)**

```python
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


def test_to_lc_messages_maps_roles(tmp_path):
    c = LLMClient(_settings(tmp_path))
    msgs = c._to_lc_messages([
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
    ])
    assert isinstance(msgs[0], SystemMessage)
    assert isinstance(msgs[1], HumanMessage)
    assert isinstance(msgs[2], AIMessage)


def test_complete_uses_invoke(tmp_path):
    c = LLMClient(_settings(tmp_path))
    c._client = _StubModel(invoke_returns=AIMessage(content="hello"))
    assert c.complete("sys", "u") == "hello"


class _StubModel:
    """最小 ChatModel 替身,记录 invoke 入参。"""
    def __init__(self, invoke_returns=None, stream_chunks=None):
        self.invoke_returns = invoke_returns
        self.stream_chunks = stream_chunks or []
        self.invoked_with = None

    def invoke(self, messages, **kw):
        self.invoked_with = messages
        return self.invoke_returns

    def stream(self, messages, **kw):
        for ch in self.stream_chunks:
            yield ch

    def bind_tools(self, tools, **kw):
        return self
```

(把 `_StubModel` 放在 `test_to_lc_messages_maps_roles` 之前,或文件顶部 import 区下。)

- [ ] **Step 2: 运行,确认 FAIL**

Run: `cd backend && python -m pytest tests/test_llm_langchain.py::test_to_lc_messages_maps_roles tests/test_llm_langchain.py::test_complete_uses_invoke -v`
Expected: FAIL(`_to_lc_messages` 不存在 / `complete` 仍走旧 `_openai_text`)。

- [ ] **Step 3: 实现 `_to_lc_messages` 并重写 `complete`**

在 `client.py` 顶部 import 追加:

```python
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
)
```

在 `LLMClient` 内(消息归一化区)新增:

```python
    def _to_lc_messages(self, msgs: list[Message]) -> list:
        """把 openai 风格 role/content dict 转为 langchain BaseMessage。"""
        out = []
        for m in msgs:
            role = m.get("role")
            content = m.get("content")
            if role == "system":
                out.append(SystemMessage(content=content))
            elif role == "user":
                out.append(HumanMessage(content=content))
            elif role == "assistant":
                out.append(AIMessage(content=content))
            else:
                # 未知角色兜底为 user 消息(tool 结果由 complete_with_tools 用 ToolMessage 单独处理)
                out.append(HumanMessage(content=content))
        return out
```

把 `complete` 方法整体替换为:

```python
    def complete(
        self, system: str, user: str, *, messages: list[Message] | None = None
    ) -> str | None:
        if self._client is None:
            self.logger.info("llm complete skipped disabled")
            return None
        msgs = self._normalize_messages(system, user, messages)
        self.logger.info(
            "llm complete start provider=%s model=%s messages=%s",
            self.settings.agent_provider,
            self.settings.agent_model,
            len(msgs),
        )
        try:
            response = self._client.invoke(self._to_lc_messages(msgs))
        except Exception:
            self.logger.exception("llm complete failed model=%s", self.settings.agent_model)
            raise
        content = response.content
        if not isinstance(content, str):
            # 多模态/tool 回包:取文本块拼接
            content = "".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
        self.logger.info("llm complete finish answer_chars=%s", len(content))
        return content
```

同时**删除** `_openai_text`、`_anthropic_text` 两个方法(它们只被旧 `complete` 调用,现已重写)。**保留** `_split_system` / `_strip_system`——它们仍被旧 `complete_with_tools`(经 `_strip_system`→`_split_system`)调用,Task 9 重写 `complete_with_tools` 后一并删除。

- [ ] **Step 4: 运行测试**

Run: `cd backend && python -m pytest tests/test_llm_langchain.py -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/src/porto_chatbot/llm/client.py backend/tests/test_llm_langchain.py
git commit -m "refactor(llm): complete 走 langchain invoke + 消息转换"
```

---

### Task 7: `stream()` 重写

**Files:**
- Modify: `backend/src/porto_chatbot/llm/client.py`(`stream`)

- [ ] **Step 1: 写失败测试(追加 test_llm_langchain.py)**

```python
def test_stream_yields_string_deltas(tmp_path):
    from langchain_core.messages import AIMessageChunk
    c = LLMClient(_settings(tmp_path))
    c._client = _StubModel(stream_chunks=[
        AIMessageChunk(content="he"), AIMessageChunk(content="llo"),
    ])
    assert "".join(c.stream("sys", "u")) == "hello"
```

- [ ] **Step 2: 运行,确认 FAIL**

Run: `cd backend && python -m pytest tests/test_llm_langchain.py::test_stream_yields_string_deltas -v`
Expected: FAIL(旧 `stream` 走 provider 分支,`_StubModel` 无 `messages.stream`)。

- [ ] **Step 3: 重写 `stream`**

替换 `stream` 方法为:

```python
    def stream(
        self, system: str, user: str, *, messages: list[Message] | None = None
    ) -> Iterator[str]:
        if self._client is None:
            self.logger.info("llm stream skipped disabled")
            return
        msgs = self._normalize_messages(system, user, messages)
        self.logger.info(
            "llm stream start provider=%s model=%s messages=%s",
            self.settings.agent_provider,
            self.settings.agent_model,
            len(msgs),
        )
        try:
            for chunk in self._client.stream(self._to_lc_messages(msgs)):
                delta = chunk.content
                if isinstance(delta, str) and delta:
                    yield delta
        except Exception:
            self.logger.exception("llm stream failed model=%s", self.settings.agent_model)
            raise
```

- [ ] **Step 4: 运行测试**

Run: `cd backend && python -m pytest tests/test_llm_langchain.py::test_stream_yields_string_deltas -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/src/porto_chatbot/llm/client.py backend/tests/test_llm_langchain.py
git commit -m "refactor(llm): stream 走 langchain ChatModel.stream"
```

---

### Task 8: `complete_structured()` 重写

**Files:**
- Modify: `backend/src/porto_chatbot/llm/client.py`(`complete_structured`,底层已是 `complete`,基本不动;只确认仍走 `complete`)

**说明**:`complete_structured` 的实现是 `complete(注入 schema 的 system) + _try_parse_json + 重试一次`,**完全复用** Task 6 的 `complete`。无需改逻辑,只需补一个针对 langchain 的回归测试。

- [ ] **Step 1: 写测试(追加 test_llm_langchain.py)**

```python
def test_structured_parses_and_retries(tmp_path):
    c = LLMClient(_settings(tmp_path))
    responses = iter([AIMessage(content="not json"), AIMessage(content='{"score": 7}')])

    def _invoke(msgs, **kw):
        return next(responses)

    c._client = type("_M", (), {"invoke": staticmethod(_invoke)})()
    parsed = c.complete_structured("sys", "u", {"type": "object"})
    assert parsed == {"score": 7}
```

- [ ] **Step 2: 运行**

Run: `cd backend && python -m pytest tests/test_llm_langchain.py::test_structured_parses_and_retries -v`
Expected: PASS(`complete_structured` 未改逻辑,底层 `complete` 已是 langchain)。

- [ ] **Step 3: 提交**

```bash
git add backend/tests/test_llm_langchain.py
git commit -m "test(llm): complete_structured langchain 适配回归"
```

---

### Task 9: `complete_with_tools()` 重写(`bind_tools` 循环)

**Files:**
- Modify: `backend/src/porto_chatbot/llm/client.py`(`complete_with_tools`;删除 `_provider_tool_step`/`_openai_tool_step`/`_anthropic_tool_step`/`_append_assistant_tool_step`/`_append_tool_result`/`_strip_system`)

**Interfaces:**
- Produces: `complete_with_tools` 内部用 `self._client.bind_tools([...]).invoke(...)`,工具 schema 取自 `ToolDef.input_schema`,handler 手动调,对话历史用 langchain `AIMessage`/`ToolMessage`。

- [ ] **Step 1: 写失败测试(追加 test_llm_langchain.py)**

```python
from porto_chatbot.llm import ToolDef


def _t(name, handler):
    return ToolDef(name=name, description="d", input_schema={"type": "object", "properties": {}, "required": []}, handler=handler)


def test_with_tools_no_tool_call_returns_text(tmp_path):
    c = LLMClient(_settings(tmp_path))
    bound = type("_B", (), {
        "invoke": lambda self, m, **k: AIMessage(content="final"),
        "bound_tools": [],
    })()
    c._client = type("_M", (), {"bind_tools": lambda self, t: bound})()
    r = c.complete_with_tools("sys", "u", [_t("noop", lambda a: "x")])
    assert r.text == "final"
    assert r.tool_calls == []
    assert r.turns == 1


def test_with_tools_executes_then_finishes(tmp_path):
    seen = []
    script = iter([
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "echo", "args": {"q": "hi"}, "type": "tool_call"}]),
        AIMessage(content="done"),
    ])
    bound = type("_B", (), {"invoke": lambda self, m, **k: next(script)})()
    c._client = type("_M", (), {"bind_tools": lambda self, t: bound})()
    r = c.complete_with_tools("sys", "u", [_t("echo", lambda a: seen.append(a) or f"echoed:{a['q']}")])
    assert r.text == "done"
    assert r.turns == 2
    assert r.tool_calls[0].name == "echo"
    assert r.tool_calls[0].arguments == {"q": "hi"}
    assert r.tool_calls[0].result == "echoed:hi"
    assert seen == [{"q": "hi"}]


def test_with_tools_unknown_tool_records_error(tmp_path):
    script = iter([
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "ghost", "args": {}, "type": "tool_call"}]),
        AIMessage(content="recovered"),
    ])
    bound = type("_B", (), {"invoke": lambda self, m, **k: next(script)})()
    c._client = type("_M", (), {"bind_tools": lambda self, t: bound})()
    r = c.complete_with_tools("sys", "u", [_t("real", lambda a: "ok")])
    assert r.tool_calls[0].result.startswith("错误：未知工具 ghost")
```

- [ ] **Step 2: 运行,确认 FAIL**

Run: `cd backend && python -m pytest tests/test_llm_langchain.py -k with_tools -v`
Expected: FAIL(旧 `complete_with_tools` 走 `_provider_tool_step`)。

- [ ] **Step 3: 重写 `complete_with_tools` + 清理**

在 `client.py` import 追加 `ToolMessage`:

```python
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
```

替换 `complete_with_tools` 整个方法为:

```python
    def complete_with_tools(
        self,
        system: str,
        user: str,
        tools: list[ToolDef],
        *,
        messages: list[Message] | None = None,
        max_turns: int | None = None,
    ) -> ToolLoopResult:
        if self._client is None:
            self.logger.info("llm complete_with_tools skipped disabled")
            return ToolLoopResult(text="")
        if not tools:
            return ToolLoopResult(text=self.complete(system, user, messages=messages) or "")

        tool_specs = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in tools
        ]
        bound = self._client.bind_tools(tool_specs)
        handlers = {t.name: t for t in tools}
        resolved_turns = max_turns or self.settings.agent_max_tool_turns
        result = ToolLoopResult()

        convo = self._to_lc_messages(self._normalize_messages(system, user, messages))
        assistant_text = ""

        for turn in range(1, resolved_turns + 1):
            result.turns = turn
            response = bound.invoke(convo)
            tool_calls = response.tool_calls or []
            assistant_text = response.content if isinstance(response.content, str) else ""
            if not tool_calls:
                result.text = assistant_text
                self.logger.info(
                    "llm tool loop stop reason=no_tool_calls turns=%s total=%s",
                    turn, len(result.tool_calls),
                )
                return result

            convo.append(response)  # AIMessage(含 tool_calls)
            for tc in tool_calls:
                name = tc["name"]
                args = tc.get("args") or {}
                tool_def = handlers.get(name)
                if tool_def is None:
                    outcome = f"错误：未知工具 {name}"
                else:
                    try:
                        outcome = tool_def.handler(args)
                    except Exception as exc:  # noqa: BLE001
                        self.logger.exception("llm tool handler failed name=%s", name)
                        outcome = f"错误：工具 {name} 执行失败：{exc}"
                result.tool_calls.append(ToolCall(name=name, arguments=args, result=outcome))
                convo.append(ToolMessage(content=outcome, tool_call_id=tc["id"]))
                self.logger.info(
                    "llm tool call name=%s args_keys=%s result_chars=%s",
                    name, list(args.keys()), len(outcome),
                )

        result.truncated = True
        # 收尾:max_turns 达到时,基于 tool loop 历史(无 bind_tools)让 LLM 给最终文本。
        # 不用 complete(system,"")——那会丢掉 convo 里的 tool 历史(旧版用 _strip_system(convo) 保留)。
        if not assistant_text:
            try:
                final_resp = self._client.invoke(convo)
                content = final_resp.content
                assistant_text = content if isinstance(content, str) else "".join(
                    b.get("text", "") for b in content if isinstance(b, dict)
                )
            except Exception:
                self.logger.exception("llm tool loop final invoke failed")
                assistant_text = ""
        result.text = assistant_text
        self.logger.warning(
            "llm tool loop truncated max_turns=%s total=%s",
            resolved_turns, len(result.tool_calls),
        )
        return result
```

同时**删除**:`_provider_tool_step`、`_openai_tool_step`、`_anthropic_tool_step`、`_append_assistant_tool_step`、`_append_tool_result`、`_strip_system`、`_split_system`(Task 6 保留至此,现全部无人调用)。

- [ ] **Step 4: 运行测试**

Run: `cd backend && python -m pytest tests/test_llm_langchain.py -k with_tools -v`
Expected: 3 个用例全 PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/src/porto_chatbot/llm/client.py backend/tests/test_llm_langchain.py
git commit -m "refactor(llm): complete_with_tools 走 bind_tools 循环,删 provider 适配"
```

---

### Task 10: `complete_document()` 重写

**结论:方案 B(保留原生 SDK)。** §Spike Conclusions U4 已确认——`backend/.env` 的 `LANGCHAIN_API_KEY`/`LANGCHAIN_BASE_URL` 实测为空,PDF spike 无法调真 LLM 验证 langchain 多模态路径,按设计 D10 走方案 B(`complete_document` 保留原生 SDK)。下方方案 A 的代码保留供未来配好 key 后切换。Task 10 的 Step 3(方案 A 测试)跳过,直接做替代 Step(方案 B:保留原生 + 补 mock 测试)。

**Files:**
- Modify: `backend/src/porto_chatbot/llm/client.py`(`complete_document`)

- [ ] **Step 1(方案 A — langchain 多模态):写测试**

追加到 `test_llm_langchain.py`:

```python
import base64

def test_complete_document_openai_multimodal(tmp_path):
    c = LLMClient(_settings(tmp_path, agent_provider="openai"))
    c._client = _StubModel(invoke_returns=AIMessage(content="# PRD"))
    out = c.complete_document("prd.pdf", b"%PDF", "application/pdf", "parse")
    assert out == "# PRD"
```

- [ ] **Step 2(方案 A):重写 `complete_document`**

```python
    def complete_document(
        self, filename: str, data: bytes, media_type: str, prompt: str
    ) -> str | None:
        """Analyze one document via langchain multimodal HumanMessage."""
        if not self.document_capabilities.native_pdf:
            return None
        encoded = base64.standard_b64encode(data).decode("ascii")
        if self.settings.agent_provider == "openai":
            content = [
                {"type": "file", "file": {"filename": filename, "file_data": f"data:{media_type};base64,{encoded}"}},
                {"type": "text", "text": prompt},
            ]
        else:  # anthropic
            content = [
                {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": encoded}},
                {"type": "text", "text": prompt},
            ]
        from langchain_core.messages import HumanMessage
        response = self._client.invoke([HumanMessage(content=content)])
        text = response.content
        if not isinstance(text, str):
            text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
        return text
```

(`base64` 需在 client.py 顶部 import,原有 `import base64` 保留。)

- [ ] **Step 3(方案 A):运行**

Run: `cd backend && python -m pytest tests/test_llm_langchain.py::test_complete_document_openai_multimodal -v`
Expected: PASS。

- [ ] **替代 Step(方案 B — 已定,U4 未验证):** `complete_document` 保留原生 SDK 路径,但因 `self._client` 现已是 langchain `ChatModel`(Task 5),`complete_document` 要改用一个**单独的原生 SDK client**(`self._native_client`),不能再调 `self._client`:

```python
# client.py:新增原生 client 构造(复用 Task 5 删掉的旧 _build_client 逻辑)
def _build_native_client(self):
    if not self.settings.agent_api_key:
        return None
    kwargs = {"api_key": self.settings.agent_api_key, "timeout": self.settings.agent_request_timeout}
    if self.settings.agent_base_url:
        kwargs["base_url"] = self.settings.agent_base_url
    if self.settings.agent_provider == "openai":
        return OpenAI(**kwargs)
    if self.settings.agent_provider == "anthropic":
        return Anthropic(**kwargs)
    return None
# __init__ 末尾: self._native_client = self._build_native_client()
```

`complete_document` 内:`if self._native_client is None: return None`(放在 native_pdf 检查之后);把 `self._client.chat.completions.create(...)` → `self._native_client.chat.completions.create(...)`,`self._client.messages.create(...)` → `self._native_client.messages.create(...)`。文件顶部加注释 `# complete_document 保留原生 SDK:U4 未验证 langchain 多模态 PDF(设计 D10)`。补 mock 测试到 `test_llm_langchain.py`:mock `client._native_client` 的 document API,断言 openai 走 `file` block / anthropic 走 `document` block + 返回文本(参照旧 test_llm_modern.py 的 document 断言风格)。**此方案下 `import anthropic/openai` 不从 dependencies 删除。**

- [ ] **Step 4: 提交**

```bash
git add backend/src/porto_chatbot/llm/client.py backend/tests/test_llm_langchain.py
git commit -m "refactor(llm): complete_document langchain 多模态"
```
(方案 B 时 commit message 改为 `refactor(llm): complete_document 保留原生 SDK(D10 fallback)`。)

---

### Task 11: `test_llm_modern.py` / `test_llm_timeout.py` 适配(mock 换 langchain)

**说明**:这两个文件用 `FakeOpenAI`/`FakeAnthropic` mock 原生 SDK,底层换 langchain 后全部失效。把它们改为用 langchain `AIMessage` 为基础的 fake(`_StubModel` 或等价),**行为级断言不变**。

**Files:**
- Modify: `backend/tests/test_llm_modern.py`
- Modify: `backend/tests/test_llm_timeout.py`

- [ ] **Step 1: 重写 test_llm_modern.py 的 fake 基座**

删除 `FakeFunction`/`FakeToolCall`/`FakeMessage`/`FakeResponse`/`FakeStreamChunk`/`FakeCompletions`/`FakeChat`/`FakeOpenAI`/`FakeAnthropicMessages`/`FakeAnthropic` 及 `_wire`,替换为基于 `AIMessage` 的 fake:

```python
import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from porto_chatbot.llm import LLMClient, ToolDef, _try_parse_json
from porto_chatbot.settings import Settings


class ScriptedModel:
    """按脚本返回 invoke/stream 结果的最小 ChatModel 替身。

    - invoke 序列:每轮弹一个 AIMessage(可带 tool_calls)
    - stream 序列:弹 AIMessageChunk 列表
    - bind_tools 返回自身(LLMClient 在 bound 上 invoke)
    """

    def __init__(self, invoke_script=None, stream_script=None):
        self._invoke = list(invoke_script or [])
        self._stream = list(stream_script or [])
        self.invoke_calls: list = []
        self.bound_tools = None

    def invoke(self, messages, **kw):
        self.invoke_calls.append(messages)
        if not self._invoke:
            raise AssertionError("ScriptedModel invoke script exhausted")
        return self._invoke.pop(0)

    def stream(self, messages, **kw):
        text = self._stream[0].content if self._stream else ""
        for i in range(0, len(text), 3):
            yield AIMessageChunk(content=text[i : i + 3])

    def bind_tools(self, tools, **kw):
        self.bound_tools = tools
        return self


@pytest.fixture()
def enabled_settings(tmp_path):
    return Settings(
        kb_dirs=[tmp_path / "kb"], data_dir=tmp_path / "data", log_dir=tmp_path / "logs",
        agent_api_key="k", agent_provider="openai", agent_model="m",
    )


@pytest.fixture()
def client(enabled_settings):
    c = LLMClient(enabled_settings)
    return c


def _wire(client: LLMClient, invoke_script, stream_script=None):
    m = ScriptedModel(invoke_script=invoke_script, stream_script=stream_script)
    client._client = m
    return m
```

- [ ] **Step 2: 重写各用例的脚本(断言语义不变)**

逐个用例替换(示例,其余同理):

```python
def test_disabled_client_returns_none(tmp_path):
    s = Settings(kb_dirs=[tmp_path / "kb"], data_dir=tmp_path / "data", log_dir=tmp_path / "logs")
    c = LLMClient(s)
    assert c.enabled is False
    assert c.complete("sys", "u") is None
    assert c.complete_structured("sys", "u", {"type": "object"}) is None
    assert c.complete_with_tools("sys", "u", []).text == ""
    assert list(c.stream("sys", "u")) == []


def test_complete_legacy_system_user(client):
    _wire(client, [AIMessage(content="hello")])
    assert client.complete("sys", "u") == "hello"


def test_complete_accepts_messages(client):
    _wire(client, [AIMessage(content="ok")])
    result = client.complete("ignored", "ignored", messages=[
        {"role": "system", "content": "s"}, {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"}, {"role": "user", "content": "u2"},
    ])
    assert result == "ok"
    sent = client._client.invoke_calls[0]
    assert isinstance(sent[-1], type(sent[-1]))  # 消息列表已转 BaseMessage
    assert sent[-1].content == "u2"
    assert len(sent) == 4


def test_structured_parses_clean_json(client):
    _wire(client, [AIMessage(content='{"score": 10, "verdict": "PASS"}')])
    assert client.complete_structured("sys", "u", {"type": "object"}) == {"score": 10, "verdict": "PASS"}


def test_structured_retries_then_succeeds(client):
    _wire(client, [AIMessage(content="not json"), AIMessage(content='{"score": 9}')])
    assert client.complete_structured("sys", "u", {"type": "object"}) == {"score": 9}
    assert len(client._client.invoke_calls) == 2


def test_structured_returns_none_after_failed_retry(client):
    _wire(client, [AIMessage(content="nope"), AIMessage(content="still nope")])
    assert client.complete_structured("sys", "u", {"type": "object"}) is None
```

tool loop 用例:

```python
def _tool(name, handler):
    return ToolDef(name=name, description="d", input_schema={"type": "object", "properties": {}, "required": []}, handler=handler)


def test_tool_loop_no_tool_call_returns_text(client):
    _wire(client, [AIMessage(content="final answer")])
    r = client.complete_with_tools("sys", "u", [_tool("noop", lambda a: "x")])
    assert r.text == "final answer" and r.tool_calls == [] and r.turns == 1


def test_tool_loop_executes_then_finishes(client):
    seen = []
    _wire(client, [
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "echo", "args": {"q": "hi"}, "type": "tool_call"}]),
        AIMessage(content="done"),
    ])
    r = client.complete_with_tools("sys", "u", [_tool("echo", lambda a: seen.append(a) or f"echoed:{a['q']}")])
    assert r.text == "done" and r.turns == 2
    assert r.tool_calls[0].name == "echo" and r.tool_calls[0].result == "echoed:hi"
    assert seen == [{"q": "hi"}]


def test_tool_loop_unknown_tool_records_error(client):
    _wire(client, [
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "ghost", "args": {}, "type": "tool_call"}]),
        AIMessage(content="recovered"),
    ])
    r = client.complete_with_tools("sys", "u", [_tool("real", lambda a: "ok")])
    assert r.tool_calls[0].result.startswith("错误：未知工具 ghost")


def test_tool_loop_truncated_at_max_turns(client, enabled_settings):
    _wire(client, [
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "loop", "args": {}, "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"id": "c2", "name": "loop", "args": {}, "type": "tool_call"}]),
        AIMessage(content="final"),
    ])
    r = client.complete_with_tools("sys", "u", [_tool("loop", lambda a: "again")], max_turns=2)
    assert r.truncated is True and r.turns == 2 and len(r.tool_calls) == 2 and r.text == "final"


def test_stream_yields_deltas(client):
    _wire(client, [], stream_script=[AIMessage(content="hello world")])
    chunks = list(client.stream("sys", "u"))
    assert "".join(chunks) == "hello world" and len(chunks) >= 2
```

保留 `test_try_parse_json_*` 全部不动(与 LLM 无关)。`test_openai_document_completion_sends_pdf_file_block` 与 `test_anthropic_document_completion_sends_document_block` **删除**——`complete_document` 的覆盖已由 `test_llm_langchain.py`(Task 10)接手,重复保留只会增加 mock 维护负担。`test_document_capabilities_require_enabled_supported_model` **保留不动**:`document_capabilities` 只读 settings + 模型名字符串、不调 LLM,`_wire(client, [])` 换成 `ScriptedModel([])` 后该用例自动适配。

- [ ] **Step 3: 适配 test_llm_timeout.py**

该文件验证 `LLMClient` 把 `agent_request_timeout` 传到底层。改为断言 `ChatOpenAI(...).model_kwargs` 或构造后 `client._client` 的 timeout 设置(具体属性依 langchain 版本,用 `getattr` 宽松断言):

```python
from porto_chatbot.llm import LLMClient
from porto_chatbot.settings import Settings


def test_timeout_passed_to_chat_model(tmp_path):
    s = Settings(
        kb_dirs=[tmp_path / "kb"], data_dir=tmp_path / "data", log_dir=tmp_path / "logs",
        agent_api_key="k", agent_provider="openai", agent_model="m", agent_request_timeout=77,
    )
    c = LLMClient(s)
    # langchain ChatOpenAI 把 timeout 存于 request_timeout 或 max_retries 相邻字段
    assert getattr(c._client, "request_timeout", None) == 77 or getattr(c._client, "timeout", None) == 77
```

- [ ] **Step 4: 运行全部 LLM 测试**

Run: `cd backend && python -m pytest tests/test_llm_modern.py tests/test_llm_timeout.py tests/test_llm_langchain.py -v`
Expected: 全 PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/tests/test_llm_modern.py backend/tests/test_llm_timeout.py
git commit -m "test(llm): mock 基座从原生 SDK 换 langchain AIMessage"
```

---

### Task 12: 全量回归 + 清理废弃依赖

**Files:**
- Modify: `backend/pyproject.toml`(若方案 A,删 `anthropic`/`openai` 原生依赖;方案 B 保留)

- [ ] **Step 1: 全量测试**

Run: `cd backend && python -m pytest -q`
Expected: 全绿(原有 87 用例 + L1 新增,允许总数变化但 0 fail)。

- [ ] **Step 2: ruff**

Run: `cd backend && python -m ruff check src tests`
Expected: clean。

- [ ] **Step 3: 降级路径冒烟(无 key)**

Run:
```bash
cd backend && PORTO_CHATBOT_DATA_DIR=/tmp/porto_l1_smoke python -c "
from porto_chatbot.settings import Settings
from porto_chatbot.llm import LLMClient
s = Settings(kb_dirs=['/tmp'], data_dir='/tmp/porto_l1_smoke', log_dir='/tmp/porto_l1_smoke')
c = LLMClient(s)
assert c.enabled is False
assert c.complete('s','u') is None
assert c.complete_with_tools('s','u',[]).text == ''
print('degradation ok')
"
```
Expected: 输出 `degradation ok`。

- [ ] **Step 4: 清理原生依赖(仅方案 A)**

若 Task 10 选方案 A(`complete_document` 全 langchain),从 `backend/pyproject.toml` 删除 `"anthropic>=0.60.0"` 与 `"openai>=1.90.0"` 两行,再 `uv sync` / `pip install -e .`,重跑 Step 1 确认仍全绿(注意:`documents.py` / `health.py` 若仍直接 import `anthropic`/`openai`,本步**跳过**——它们属检索/健康探测层,不在 L1 范围,保留依赖)。

Run: `cd backend && grep -rn "from anthropic\|from openai\|import anthropic\|import openai" src --include="*.py"`
Expected: 若命中 `documents.py`/`health.py` 等 L1 范围外文件,**不删依赖**;若仅 `llm/client.py` 命中且已无,才删。

- [ ] **Step 5: 提交**

```bash
git add backend/pyproject.toml backend/uv.lock
git commit -m "chore(backend): L1 全量回归通过,清理原生 SDK 依赖(若适用)"
```

---

## Spike Conclusions

> 执行 Task 2/3/4/10 时把结论填到这里,并同步回设计文档 §11。L2/L3 plan 据此细化。

- **U1 (Send → 子图 → reducer map-reduce)**: _待填_(Task 3)
- **U2 (sync Send 并行性)**: _待填_(Task 3,观察耗时/线程)
- **U3 (SqliteSaver 多线程)**: _待填_(Task 4)
- **U4 (ChatModel PDF document)**: 未验证 → **Task 10 方案 B**。`backend/.env` 的 `LANGCHAIN_API_KEY`/`LANGCHAIN_BASE_URL` 实测为空(Task 2 发现),spike 无法调真 LLM;按 D10 走方案 B(`complete_document` 保留原生 SDK)。未来配 key 后可补验证切方案 A。

## L1 完成判据

- `cd backend && python -m pytest -q` 全绿,ruff clean。
- `LLMClient` 6 方法 + 2 属性签名未改(`git diff backend/src/porto_chatbot/llm/types.py` 为空)。
- 8 处调用方文件 `git diff` 为空(chat/intent/memory/specs/nodes/documents/workflow 路由/workflow_executor)。
- settings.py `git diff` 为空。
- 无 key 降级冒烟通过。

## 后续(L2/L3,本 plan 范围外)

依 §Spike Conclusions 各自开 plan:
- **L2**: `WorkflowRunner` → langgraph `StateGraph` + `SqliteSaver` + `update_state`(PUT/PATCH)+ `WorkflowStore` 投影瘦身。
- **L3**: spec refine 子图(initial/critique/decide/refine)+ generate 节点 `Send` map-reduce(U1/U2 结论定调度方式)。
