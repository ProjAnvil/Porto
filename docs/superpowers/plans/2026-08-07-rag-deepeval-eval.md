# RAG DeepEval 质量评测门禁 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Porto RAG 管线建一套 DeepEval LLM-as-judge 回归门禁，以公开中文数据集 DomainRAG 为评测语料，全链路（检索+生成+judge）跑 6 项指标，pytest 集成。

**Architecture:** 全部新代码在 `backend/tests/rag_eval/`（不进 `src/`）。门禁直接编排 `retrieve_with_transform`（检索）+ `LLMClient.complete`（生成），组装 DeepEval `LLMTestCase`，跑 6 指标，批量均值判定。数据集经 Makefile 下载脚本落到 gitignored `data/`。judge LLM 复用 Porto 的 OpenAI-compatible endpoint，配置直读 `.env.test`（绕过根 conftest 的 LLM key 隔离）。

**Tech Stack:** Python 3.12+、pytest、DeepEval（`[eval]` 可选依赖）、gdown、Porto 自身（`ChromaVectorStore`/`retrieve_with_transform`/`LLMClient`/`Settings`）。

## Global Constraints

- **零侵入**：不动 `src/`、不动 `evaluation.py`/`api/routes/eval.py`/`tests/test_memory_eval.py`、不动根 `tests/conftest.py`。仅新增 `backend/tests/rag_eval/**` + 改 `pyproject.toml`/`Makefile`/`.gitignore`。
- **依赖隔离**：`deepeval`/`gdown` 只进 `[project.optional-dependencies] eval`，不进 runtime deps。`metrics.py` 的 `aggregate`/`THRESHOLDS` 必须在**未装 deepeval** 时仍可 import（deepeval 延迟导入）。
- **确定性优先**：eval KB 用 `embedding_provider="local"`、`embedding_dimensions=128`；eval Settings 的 `data_dir`/`kb_dirs`/`vector_collection` 全部隔离到 tmp。
- **集成标记**：`test_rag_gate.py` 用 `@pytest.mark.integration`（`pyproject.toml` 已定义该 marker）。
- **代码风格**：`ruff line-length=100`、`from __future__ import annotations`、loguru 风格的 `get_component_logger` 不用于 test 侧（直接 print/logging 即可）。
- **提交粒度**：每个 Task 末尾一次 commit，信息以 `feat(eval):`/`test(eval):`/`chore(eval):` 开头。

---

## File Structure

| 文件 | 职责 | 由哪个 Task 创建 |
|---|---|---|
| `backend/pyproject.toml` | 加 `[eval]` 可选依赖 | T1 |
| `.gitignore` | 加 `backend/tests/rag_eval/data/` | T1 |
| `Makefile` | 加 `eval-install`/`eval-dataset`/`eval-test` | T1 |
| `backend/tests/rag_eval/__init__.py` | 包标识（空） | T1 |
| `backend/tests/rag_eval/schema.py` | `CorpusDoc`/`RagCorpus`/`RagGolden` 归一化 schema | T1 |
| `backend/tests/rag_eval/synth/*.md` + `synth/goldens.json` | 迷你合成语料（committed） | T2 |
| `backend/tests/rag_eval/provision.py` | `build_eval_kb()` → `EvalKb(store, settings)` | T2 |
| `backend/tests/rag_eval/runner.py` | `run_rag()` → `LLMTestCase` | T3 |
| `backend/tests/rag_eval/metrics.py` | `THRESHOLDS`/`build_metrics`/`evaluate_case`/`aggregate` | T4 |
| `backend/tests/rag_eval/loaders/__init__.py` | 包标识（空） | T5 |
| `backend/tests/rag_eval/loaders/domainrag.py` | `load_domainrag()` → `(RagCorpus, list[RagGolden])` | T5 |
| `backend/tests/rag_eval/scripts/__init__.py` | 包标识（空） | T5 |
| `backend/tests/rag_eval/scripts/fetch_dataset.py` | gdown 下载 DomainRAG | T5 |
| `backend/tests/rag_eval/conftest.py` | session fixtures：`eval_kb`/`goldens`/judge env | T6 |
| `backend/tests/rag_eval/test_rag_gate.py` | 集成门禁（report-only 起步） | T6 |
| `backend/tests/rag_eval/test_harness.py` | 廉价单测（随各 Task 累积） | T1–T5 |
| `backend/tests/rag_eval/data/` | **gitignored**，DomainRAG 落地处 | T5 脚本运行后 |

---

### Task 1: 脚手架 + 依赖 + schema

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `.gitignore`
- Modify: `Makefile`
- Create: `backend/tests/rag_eval/__init__.py`, `backend/tests/rag_eval/schema.py`, `backend/tests/rag_eval/test_harness.py`

**Interfaces:**
- Produces: `schema.CorpusDoc(id:str,text:str,metadata:dict)`, `schema.RagCorpus(docs:list[CorpusDoc])`, `schema.RagGolden(question:str, reference_answer:str, gold_doc_ids:list[str], category:str="default")`

- [ ] **Step 1: 加 `[eval]` 可选依赖**

Modify `backend/pyproject.toml` 的 `[project.optional-dependencies]` 改为：

```toml
[project.optional-dependencies]
document-ai = [
    "docling>=2.0.0",
]
eval = [
    "deepeval>=2.5.0",
    "gdown>=2.0.0",
]
```

- [ ] **Step 2: 加 .gitignore 条目**

在 `.gitignore` 末尾追加：

```
# RAG eval dataset (downloaded via make eval-dataset)
backend/tests/rag_eval/data/
backend/tests/rag_eval/.last_report.json
```

- [ ] **Step 3: 加 Makefile targets**

在 `Makefile` 顶部 `.PHONY` 行追加 `eval-install eval-dataset eval-test`；在文件末尾追加（**recipe 行必须以 TAB 开头**）：

```make
eval-install: ## 安装 RAG 评测可选依赖 (deepeval, gdown)
	cd backend && pip install -e ".[eval]"

eval-dataset: ## 下载 DomainRAG 评测数据集到 gitignored 目录
	cd backend && python -m tests.rag_eval.scripts.fetch_dataset

eval-test: ## 运行 DeepEval RAG 质量门禁 (需 LLM key + 数据集)
	cd backend && pytest -m integration tests/rag_eval/test_rag_gate.py
```

> 注：`eval-dataset` 在 T5 才有 `fetch_dataset.py`；此处先登记 target，T5 后可运行。

- [ ] **Step 4: 写 schema 失败测试**

`backend/tests/rag_eval/test_harness.py`：

```python
from __future__ import annotations

from tests.rag_eval.schema import CorpusDoc, RagCorpus, RagGolden


def test_schema_constructs():
    corpus = RagCorpus(docs=[CorpusDoc(id="d1", text="你好", metadata={"k": "v"})])
    g = RagGolden(question="Q?", reference_answer="A.", gold_doc_ids=["d1"])
    assert corpus.docs[0].id == "d1"
    assert corpus.docs[0].text == "你好"
    assert g.category == "default"
    assert g.gold_doc_ids == ["d1"]
```

- [ ] **Step 5: 运行验证失败**

Run: `cd backend && python -m pytest tests/rag_eval/test_harness.py -v`
Expected: FAIL（`ModuleNotFoundError: tests.rag_eval.schema`）

- [ ] **Step 6: 写 schema 实现**

`backend/tests/rag_eval/__init__.py`：空文件。

`backend/tests/rag_eval/schema.py`：

```python
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CorpusDoc:
    id: str
    text: str
    metadata: dict = field(default_factory=dict)


@dataclass
class RagCorpus:
    docs: list[CorpusDoc]


@dataclass
class RagGolden:
    question: str
    reference_answer: str
    gold_doc_ids: list[str]
    category: str = "default"
```

- [ ] **Step 7: 运行验证通过**

Run: `cd backend && python -m pytest tests/rag_eval/test_harness.py -v`
Expected: PASS（1 passed）

- [ ] **Step 8: ruff + 提交**

Run: `cd backend && ruff check tests/rag_eval/`
Expected: 无报错。

```bash
git add backend/pyproject.toml .gitignore Makefile backend/tests/rag_eval/
git commit -m "chore(eval): 脚手架 + [eval] 依赖 + schema"
```

---

### Task 2: 合成语料 + provision（隔离 KB build）

**Files:**
- Create: `backend/tests/rag_eval/synth/payment.md`, `synth/risk.md`, `synth/notify.md`, `synth/goldens.json`
- Create: `backend/tests/rag_eval/provision.py`
- Modify: `backend/tests/rag_eval/test_harness.py`

**Interfaces:**
- Consumes: `schema.RagCorpus`、`porto_chatbot.settings.Settings`、`porto_chatbot.vector_store.ChromaVectorStore`
- Produces: `provision.EvalKb(store: ChromaVectorStore, settings: Settings)`、`provision.build_eval_kb(corpus: RagCorpus, tmp_dir: Path, embedding_dimensions: int = 128) -> EvalKb`

- [ ] **Step 1: 写合成语料文件**

`backend/tests/rag_eval/synth/payment.md`：

```markdown
# 支付服务

payment-service 负责支付授权、退款、结算与渠道路由。
支持订单状态追踪与对账报表。
```

`backend/tests/rag_eval/synth/risk.md`：

```markdown
# 风控服务

risk-service 在高额交易前评估欺诈规则，命中规则则拦截交易。
```

`backend/tests/rag_eval/synth/notify.md`：

```markdown
# 通知服务

notification-service 负责向商户与用户发送支付结果消息。
```

`backend/tests/rag_eval/synth/goldens.json`（供 loader 风格的合成测试用，T5 复用）：

```json
[
  {"question": "支付服务负责什么？", "reference_answer": "支付授权、退款、结算与渠道路由。", "gold_doc_ids": ["payment"], "category": "extractive"},
  {"question": "高额交易前谁评估欺诈？", "reference_answer": "risk-service 评估欺诈规则并可能拦截。", "gold_doc_ids": ["risk"], "category": "extractive"}
]
```

- [ ] **Step 2: 写 provision 失败测试**

追加到 `test_harness.py`：

```python
from pathlib import Path

from tests.rag_eval.provision import build_eval_kb
from tests.rag_eval.schema import CorpusDoc, RagCorpus


def _synth_corpus(synth_dir: Path) -> RagCorpus:
    return RagCorpus(
        docs=[
            CorpusDoc(id=p.stem, text=p.read_text(encoding="utf-8"))
            for p in sorted(synth_dir.glob("*.md"))
        ]
    )


def test_build_eval_kb_indexes_and_retrieves(tmp_path):
    synth_dir = Path(__file__).parent / "synth"
    corpus = _synth_corpus(synth_dir)
    kb = build_eval_kb(corpus, tmp_path)
    # 索引非空
    assert kb.store.is_rag_ready()
    # 已知文档可被检索命中（中文查询）
    hits = kb.store.search("支付退款结算", top_k=3)
    assert hits, "检索应返回结果"
    assert any("支付" in h.text for h in hits)
    # 隔离：collection 名与默认 porto_kb 不同
    assert kb.settings.vector_collection == "eval_domainrag"
    assert kb.settings.data_dir != Path.home() / ".porto"
```

- [ ] **Step 3: 运行验证失败**

Run: `cd backend && python -m pytest tests/rag_eval/test_harness.py::test_build_eval_kb_indexes_and_retrieves -v`
Expected: FAIL（`ModuleNotFoundError: tests.rag_eval.provision`）

- [ ] **Step 4: 写 provision 实现**

`backend/tests/rag_eval/provision.py`：

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from porto_chatbot.settings import Settings
from porto_chatbot.vector_store import ChromaVectorStore

from .schema import RagCorpus

_SAFE = re.compile(r"[^a-zA-Z0-9_-]+")


def _safe_filename(doc_id: str) -> str:
    return _SAFE.sub("_", doc_id)


@dataclass
class EvalKb:
    store: ChromaVectorStore
    settings: Settings


def build_eval_kb(
    corpus: RagCorpus, tmp_dir: Path, embedding_dimensions: int = 128
) -> EvalKb:
    """把 corpus 落成隔离 kb_dir 文件 → 隔离 Settings → build()。

    全程与用户真实 KB 零交集：data_dir/chroma_dir/kb_dirs/vector_collection 都隔离。
    embedding 固定 local（确定性、零成本）。
    """
    kb_dir = tmp_dir / "eval_kb"
    kb_dir.mkdir(parents=True, exist_ok=True)
    for doc in corpus.docs:
        (kb_dir / f"{_safe_filename(doc.id)}.md").write_text(doc.text, encoding="utf-8")

    settings = Settings(
        kb_dirs=[kb_dir],
        data_dir=tmp_dir / "eval_data",
        log_dir=tmp_dir / "eval_logs",
        embedding_provider="local",
        embedding_dimensions=embedding_dimensions,
        vector_collection="eval_domainrag",
        retrieval_method="hybrid",
        rerank_enabled=False,
    )
    store = ChromaVectorStore(settings)
    store.build(reset=True)
    return EvalKb(store=store, settings=settings)
```

- [ ] **Step 5: 运行验证通过**

Run: `cd backend && python -m pytest tests/rag_eval/test_harness.py -v`
Expected: PASS（2 passed）

- [ ] **Step 6: ruff + 提交**

Run: `cd backend && ruff check tests/rag_eval/`
Expected: 无报错。

```bash
git add backend/tests/rag_eval/
git commit -m "feat(eval): 合成语料 + provision 隔离 KB build"
```

---

### Task 3: runner（检索 + 生成 → LLMTestCase）

**Files:**
- Create: `backend/tests/rag_eval/runner.py`
- Modify: `backend/tests/rag_eval/test_harness.py`

**Interfaces:**
- Consumes: `schema.RagGolden`、`provision.EvalKb`、`porto_chatbot.query_transform.retrieve_with_transform`、`porto_chatbot.llm.client.LLMClient`、`porto_chatbot.models.enums.QueryTransformStrategy`
- Produces: `runner.run_rag(golden, eval_kb, llm, *, strategy=QueryTransformStrategy.NONE, top_k=6) -> tuple[LLMTestCase, TransformResult]`

- [ ] **Step 1: 写 runner 失败测试（mock store + llm）**

追加到 `test_harness.py`：

```python
from unittest.mock import MagicMock

from porto_chatbot.models import SourceChunk
from porto_chatbot.models.enums import QueryTransformStrategy
from porto_chatbot.query_transform import TransformResult

from tests.rag_eval.runner import run_rag
from tests.rag_eval.schema import RagGolden


def test_run_rag_assembles_llm_test_case(tmp_path):
    golden = RagGolden(
        question="支付服务负责什么？",
        reference_answer="支付授权、退款、结算与渠道路由。",
        gold_doc_ids=["payment"],
    )
    eval_kb = MagicMock()
    eval_kb.store = MagicMock()
    eval_kb.settings = MagicMock()
    eval_kb.settings.rerank_enabled = False
    chunk = SourceChunk(id="c1", path="p", title="t", text="payment-service 负责支付、退款、结算", score=0.9, metadata={})
    eval_kb.store.search.return_value = [chunk]

    llm = MagicMock()
    llm.complete.return_value = "支付服务负责支付授权、退款、结算与渠道路由。"

    tc, result = run_rag(golden, eval_kb, llm, strategy=QueryTransformStrategy.NONE, top_k=3)

    # NONE 策略走 store.search 原路径
    eval_kb.store.search.assert_called_once_with(golden.question, 3)
    assert isinstance(result, TransformResult)
    # LLMTestCase 字段组装正确
    assert tc.input == golden.question
    assert tc.expected_output == golden.reference_answer
    assert tc.actual_output == llm.complete.return_value
    assert tc.retrieval_context == [chunk.text]


def test_run_rag_guards_disabled_llm(tmp_path):
    golden = RagGolden(question="Q", reference_answer="A", gold_doc_ids=["x"])
    eval_kb = MagicMock()
    eval_kb.store = MagicMock()
    eval_kb.settings = MagicMock()
    eval_kb.settings.rerank_enabled = False
    eval_kb.store.search.return_value = []
    llm = MagicMock()
    llm.complete.return_value = None  # LLM 禁用

    tc, _ = run_rag(golden, eval_kb, strategy=QueryTransformStrategy.NONE, top_k=3)
    assert "未返回" in tc.actual_output  # guard 文案
    assert tc.retrieval_context == []
```

> 已核实：`SourceChunk`（`models/common.py:15`）字段为 `id/path/title/text/score=0.0/metadata={}`，本测试构造与之完全一致。

- [ ] **Step 2: 运行验证失败**

Run: `cd backend && python -m pytest tests/rag_eval/test_harness.py -v -k run_rag`
Expected: FAIL（`ModuleNotFoundError: tests.rag_eval.runner`）

- [ ] **Step 3: 写 runner 实现**

`backend/tests/rag_eval/runner.py`：

```python
from __future__ import annotations

from deepeval.test_case import LLMTestCase

from porto_chatbot.llm.client import LLMClient
from porto_chatbot.models.enums import QueryTransformStrategy
from porto_chatbot.query_transform import TransformResult, retrieve_with_transform

from .provision import EvalKb
from .schema import RagGolden

_RAG_SYSTEM_PROMPT = (
    "你是一个严格基于所提供上下文回答问题的助手。"
    "只使用上下文中的信息作答；若上下文不足以回答，请明确说明。回答用中文，简洁。"
)


def run_rag(
    golden: RagGolden,
    eval_kb: EvalKb,
    llm: LLMClient,
    *,
    strategy: QueryTransformStrategy = QueryTransformStrategy.NONE,
    top_k: int = 6,
) -> tuple[LLMTestCase, TransformResult]:
    """检索（retrieve_with_transform）→ 生成（LLMClient.complete）→ 组装 LLMTestCase。"""
    result = retrieve_with_transform(
        golden.question, strategy, eval_kb.store, eval_kb.settings, llm, top_k
    )
    if result.chunks:
        context = "\n\n".join(f"[{i}] {c.text}" for i, c in enumerate(result.chunks))
    else:
        context = "（无相关上下文）"
    answer = llm.complete(_RAG_SYSTEM_PROMPT, user=f"问题: {golden.question}\n\n上下文:\n{context}")
    answer = answer or "（模型未返回答案）"
    tc = LLMTestCase(
        input=golden.question,
        actual_output=answer,
        expected_output=golden.reference_answer,
        retrieval_context=[c.text for c in result.chunks],
    )
    return tc, result
```

> 注：`runner.py` import `deepeval` —— 它只被 `test_rag_gate.py`（integration）调用，运行前已 `pip install -e ".[eval]"`。`test_harness.py` 测 runner 时也需 deepeval 装好（CI 装了 eval 组则 OK；否则把这些 case 标记需 eval 依赖）。若 CI 不装 eval 组，把这些 mock 测试归到一个 `eval` marker 下并 `pytest -m eval`，或接受它们需 `[eval]`。

- [ ] **Step 4: 运行验证通过**

Run: `cd backend && pip install -e ".[eval]" && python -m pytest tests/rag_eval/test_harness.py -v -k run_rag`
Expected: PASS（2 passed）

- [ ] **Step 5: ruff + 提交**

Run: `cd backend && ruff check tests/rag_eval/`

```bash
git add backend/tests/rag_eval/
git commit -m "feat(eval): runner 检索+生成→LLMTestCase"
```

---

### Task 4: metrics（阈值 + 指标集 + 聚合）

**Files:**
- Create: `backend/tests/rag_eval/metrics.py`
- Modify: `backend/tests/rag_eval/test_harness.py`

**Interfaces:**
- Produces:
  - `metrics.THRESHOLDS: dict[str,float]`
  - `metrics.build_metrics(model: str) -> dict[str, <DeepEval metric>]`（延迟导入 deepeval）
  - `metrics.evaluate_case(tc: LLMTestCase, metrics: dict) -> dict[str, float]`（调 `metric.measure`）
  - `metrics.aggregate(per_case: list[dict[str,float]]) -> dict[str, float]`（**纯函数，不依赖 deepeval**）
  - `metrics.judge(per_metric_mean: dict[str,float]) -> tuple[bool, dict]`（对比 THRESHOLDS）

- [ ] **Step 1: 写 aggregate 纯函数失败测试（无需 deepeval）**

追加到 `test_harness.py`：

```python
from tests.rag_eval.metrics import THRESHOLDS, aggregate, judge


def test_aggregate_means_per_metric():
    per_case = [
        {"faithfulness": 0.8, "answer_relevancy": 0.6},
        {"faithfulness": 0.4, "answer_relevancy": 0.8},
    ]
    agg = aggregate(per_case)
    assert agg["faithfulness"] == 0.6
    assert agg["answer_relevancy"] == 0.7


def test_judge_passes_when_all_above_threshold():
    mean = {k: v + 0.2 for k, v in THRESHOLDS.items()}
    passed, detail = judge(mean)
    assert passed
    assert all(d["passed"] for d in detail.values())


def test_judge_fails_when_any_below_threshold():
    key = next(iter(THRESHOLDS))
    mean = {k: v + 0.2 for k, v in THRESHOLDS.items()}
    mean[key] = THRESHOLDS[key] - 0.1  # 拉低一个
    passed, detail = judge(mean)
    assert not passed
    assert detail[key]["passed"] is False
```

- [ ] **Step 2: 运行验证失败**

Run: `cd backend && python -m pytest tests/rag_eval/test_harness.py -v -k "aggregate or judge"`
Expected: FAIL（`ModuleNotFoundError: tests.rag_eval.metrics`）

- [ ] **Step 3: 写 metrics 实现**

`backend/tests/rag_eval/metrics.py`：

```python
from __future__ import annotations

from typing import Any

# 批量均值门禁阈值（report-only 起步后据基线调整）。偏松防 flaky。
THRESHOLDS: dict[str, float] = {
    "faithfulness": 0.55,
    "answer_relevancy": 0.50,
    "answer_correctness": 0.45,
    "contextual_precision": 0.50,
    "contextual_recall": 0.50,
    "contextual_relevancy": 0.50,
}


def build_metrics(model: str) -> dict[str, Any]:
    """实例化 6 个 DeepEval 指标（threshold=0：逐条不判过，统一由 aggregate 批量判）。"""
    from deepeval.metrics import (
        AnswerCorrectnessMetric,
        AnswerRelevancyMetric,
        ContextualPrecisionMetric,
        ContextualRecallMetric,
        ContextualRelevancyMetric,
        FaithfulnessMetric,
    )

    return {
        "faithfulness": FaithfulnessMetric(threshold=0.0, model=model),
        "answer_relevancy": AnswerRelevancyMetric(threshold=0.0, model=model),
        "answer_correctness": AnswerCorrectnessMetric(threshold=0.0, model=model),
        "contextual_precision": ContextualPrecisionMetric(threshold=0.0, model=model),
        "contextual_recall": ContextualRecallMetric(threshold=0.0, model=model),
        "contextual_relevancy": ContextualRelevancyMetric(threshold=0.0, model=model),
    }


def evaluate_case(tc, metrics: dict[str, Any]) -> dict[str, float]:
    """对单个 LLMTestCase 跑全部指标，返回 {metric_name: score}。"""
    scores: dict[str, float] = {}
    for name, metric in metrics.items():
        metric.measure(tc)
        scores[name] = float(metric.score or 0.0)
    return scores


def aggregate(per_case: list[dict[str, float]]) -> dict[str, float]:
    """各指标跨 case 取均值。"""
    if not per_case:
        return {k: 0.0 for k in THRESHOLDS}
    keys = THRESHOLDS.keys()
    return {k: round(sum(c.get(k, 0.0) for c in per_case) / len(per_case), 4) for k in keys}


def judge(per_metric_mean: dict[str, float]) -> tuple[bool, dict[str, dict]]:
    """对比 THRESHOLDS：全指标达标才 pass。返回 (passed, {metric: {mean, threshold, passed}})。"""
    detail: dict[str, dict] = {}
    for k, thr in THRESHOLDS.items():
        mean = per_metric_mean.get(k, 0.0)
        detail[k] = {"mean": mean, "threshold": thr, "passed": mean >= thr}
    passed = all(d["passed"] for d in detail.values())
    return passed, detail
```

- [ ] **Step 4: 运行验证通过**

Run: `cd backend && python -m pytest tests/rag_eval/test_harness.py -v -k "aggregate or judge"`
Expected: PASS（3 passed）。确认未装 deepeval 时这 3 个 case 也能跑通（`build_metrics` 不被它们触发）。

- [ ] **Step 5: ruff + 提交**

Run: `cd backend && ruff check tests/rag_eval/`

```bash
git add backend/tests/rag_eval/
git commit -m "feat(eval): metrics 阈值/指标集/聚合 + 纯函数单测"
```

---

### Task 5: 数据集下载脚本 + DomainRAG loader

**Files:**
- Create: `backend/tests/rag_eval/scripts/__init__.py`, `backend/tests/rag_eval/scripts/fetch_dataset.py`
- Create: `backend/tests/rag_eval/loaders/__init__.py`, `backend/tests/rag_eval/loaders/domainrag.py`
- Create: `backend/tests/rag_eval/data/.gitkeep`（占位，让目录存在但内容被 ignore）
- Modify: `backend/tests/rag_eval/test_harness.py`

**Interfaces:**
- Produces: `loaders.domainrag.load_domainrag(data_dir: Path | None = None) -> tuple[RagCorpus, list[RagGolden]]`

> **重要前置**：DomainRAG 真实字段名未知。Task 流程：先写脚本 → `make eval-dataset` 下载 → 打开 `data/domainrag/` 核对真实字段 → 据此调整 `loaders/domainrag.py` 顶部的字段常量。loader 单测用一个**合成 raw fixture**（匹配假定的字段名），保证确定性；真实字段核对是显式人工步骤。

- [ ] **Step 1: 写 fetch 脚本**

`backend/tests/rag_eval/scripts/__init__.py`：空。

`backend/tests/rag_eval/scripts/fetch_dataset.py`：

```python
#!/usr/bin/env python3
"""下载 DomainRAG（corpus + QA）到 tests/rag_eval/data/domainrag/。

首次运行后请查看打印的落地文件列表，并据此核对 loaders/domainrag.py 的字段常量。
"""
from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "data" / "domainrag"
# DomainRAG README 所示 Google Drive 文件 id（首次运行核对；如失效去仓库 README 取新 id）。
FILE_ID = "1NquEyPGwP0MpTGJwDUUYKU37snYN4Er4"


def main() -> None:
    try:
        import gdown
    except ImportError:
        sys.exit("缺少 gdown，请先运行：make eval-install")

    TARGET.mkdir(parents=True, exist_ok=True)
    archive = TARGET / "_download"
    archive.mkdir(exist_ok=True)
    out = archive / "domainrag"
    print(f"下载 DomainRAG (file id={FILE_ID}) → {out}")
    gdown.download(id=FILE_ID, output=str(out), quiet=False)

    # 兼容 tar.gz / zip
    if tarfile.is_tarfile(out):
        with tarfile.open(out) as tar:
            tar.extractall(TARGET)
    else:
        try:
            with zipfile.ZipFile(out) as zf:
                zf.extractall(TARGET)
        except zipfile.BadZipFile:
            # 可能已是目录或未知格式，原样保留
            out.rename(TARGET / "domainrag.raw")
    print(f"完成，数据位于 {TARGET}。落地文件：")
    for p in sorted(TARGET.rglob("*")):
        if p.is_file():
            print("  ", p.relative_to(TARGET))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 占位 .gitkeep**

`backend/tests/rag_eval/data/.gitkeep`：空文件（`data/` 内容被 ignore，但目录结构可入库）。

- [ ] **Step 3: 运行脚本下载 + 核对字段**

Run: `make eval-install && make eval-dataset`
然后人工：打开 `backend/tests/rag_eval/data/domainrag/`，找到 corpus 文档文件与 QA 文件，记录真实字段名（如 corpus 的 id/title/text 字段、QA 的 query/answer/golden doc 字段）。

> 若 Google Drive 失效：去 https://github.com/ShootingWong/DomainRAG README 取新链接，更新 `FILE_ID`（或改 `gdown.download(url=...)`）后重跑。这是脚本可维护点，不是阻塞。

- [ ] **Step 4: 写 loader 失败测试（合成 raw fixture）**

`backend/tests/rag_eval/test_harness.py` 追加：

```python
import json

from tests.rag_eval.loaders.domainrag import load_domainrag_from_records


def test_domainrag_loader_normalizes(tmp_path):
    # 合成 raw 记录，匹配 loaders/domainrag.py 假定的字段名常量
    corpus_recs = [
        {"_id": "d1", "title": "支付", "text": "payment-service 负责支付退款结算。"},
        {"_id": "d2", "title": "风控", "text": "risk-service 评估欺诈。"},
    ]
    qa_recs = [
        {"query": "谁负责支付？", "answer": "payment-service。", "golden_doc_ids": ["d1"]},
    ]
    corpus, goldens = load_domainrag_from_records(corpus_recs, qa_recs)
    assert len(corpus.docs) == 2
    assert corpus.docs[0].id == "d1"
    assert len(goldens) == 1
    assert goldens[0].question == "谁负责支付？"
    assert goldens[0].gold_doc_ids == ["d1"]
```

- [ ] **Step 5: 运行验证失败**

Run: `cd backend && python -m pytest tests/rag_eval/test_harness.py -v -k domainrag`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 6: 写 loader 实现（字段常量据 Step 3 核对结果调整）**

`backend/tests/rag_eval/loaders/__init__.py`：空。

`backend/tests/rag_eval/loaders/domainrag.py`：

```python
from __future__ import annotations

import json
from pathlib import Path

from ..schema import CorpusDoc, RagCorpus, RagGolden

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "domainrag"

# === 字段映射：据 make eval-dataset 下载后核对的真实字段调整 ===
CORPUS_ID_FIELD = "_id"
CORPUS_TEXT_FIELD = "text"
QA_QUERY_FIELD = "query"
QA_ANSWER_FIELD = "answer"
QA_GOLD_FIELD = "golden_doc_ids"  # 若真实数据是单字段名/路径，这里改解析逻辑


def _load_records(path: Path) -> list[dict]:
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return json.loads(path.read_text(encoding="utf-8"))


def load_domainrag_from_records(
    corpus_recs: list[dict], qa_recs: list[dict]
) -> tuple[RagCorpus, list[RagGolden]]:
    """纯归一化：raw 记录 → schema。字段名由上方常量驱动，便于核对后一处修改。"""
    corpus = RagCorpus(
        docs=[
            CorpusDoc(id=str(r[CORPUS_ID_FIELD]), text=str(r[CORPUS_TEXT_FIELD]))
            for r in corpus_recs
        ]
    )
    goldens = [
        RagGolden(
            question=str(q[QA_QUERY_FIELD]),
            reference_answer=str(q[QA_ANSWER_FIELD]),
            gold_doc_ids=list(q.get(QA_GOLD_FIELD, [])),
        )
        for q in qa_recs
    ]
    return corpus, goldens


def load_domainrag(data_dir: Path | None = None) -> tuple[RagCorpus, list[RagGolden]]:
    """从 data_dir（默认 DATA_DIR）读取 corpus/QA 文件并归一化。

    文件名/结构据 make eval-dataset 落地结果在此处拼路径（如 corpus.json + qa.json）。
    缺失时抛 FileNotFoundError，由 conftest 捕获后 pytest.skip。
    """
    base = data_dir or DATA_DIR
    # ↓↓↓ 据真实落地文件名调整（示例占位，Step 3 核对后改实）：
    corpus_path = base / "corpus.json"
    qa_path = base / "qa.json"
    if not corpus_path.exists() or not qa_path.exists():
        raise FileNotFoundError(f"DomainRAG 数据缺失于 {base}，请运行 make eval-dataset")
    return load_domainrag_from_records(_load_records(corpus_path), _load_records(qa_path))
```

> `load_domainrag` 里的 `corpus_path`/`qa_path` 文件名是**唯一需据 Step 3 核对结果改动**处；`load_domainrag_from_records` 是确定性、被单测覆盖的核心。

- [ ] **Step 7: 运行验证通过**

Run: `cd backend && python -m pytest tests/rag_eval/test_harness.py -v -k domainrag`
Expected: PASS

- [ ] **Step 8: ruff + 提交**

Run: `cd backend && ruff check tests/rag_eval/`

```bash
git add backend/tests/rag_eval/ Makefile
git commit -m "feat(eval): DomainRAG 下载脚本 + loader（字段核对后定稿）"
```

---

### Task 6: conftest session fixtures + 集成门禁

**Files:**
- Create: `backend/tests/rag_eval/conftest.py`, `backend/tests/rag_eval/test_rag_gate.py`

**Interfaces:**
- Consumes: 全部前置 Task 的产物
- Produces: 可运行的 `pytest -m integration tests/rag_eval/test_rag_gate.py`

- [ ] **Step 1: 写 conftest（session eval_kb + goldens + judge env，从 .env.test 直读绕过隔离）**

`backend/tests/rag_eval/conftest.py`：

```python
from __future__ import annotations

import os
from pathlib import Path

import pytest

from porto_chatbot.llm.client import LLMClient
from porto_chatbot.models.enums import QueryTransformStrategy
from porto_chatbot.settings import Settings

from .loaders.domainrag import load_domainrag
from .provision import build_eval_kb

_SYNTH_DIR = Path(__file__).resolve().parent / "synth"


def _read_env_test() -> dict[str, str]:
    """直读 backend/.env.test（绕过根 conftest 的 LLM key autouse 隔离）。"""
    env_test = Path(__file__).resolve().parents[2] / ".env.test"
    out: dict[str, str] = {}
    if not env_test.exists():
        return out
    for line in env_test.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def _has_llm_key() -> bool:
    return bool(_read_env_test().get("LANGCHAIN_API_KEY"))


@pytest.fixture(scope="session")
def domainrag_data():
    """加载 DomainRAG；未下载时 pytest.skip（只影响请求本 fixture 的集成门禁）。"""
    try:
        corpus, goldens = load_domainrag()
    except FileNotFoundError:
        pytest.skip("DomainRAG 未下载 —— 运行 make eval-dataset")
    return corpus, goldens


@pytest.fixture(scope="session")
def eval_kb(tmp_path_factory, domainrag_data):
    corpus, _ = domainrag_data
    tmp = tmp_path_factory.mktemp("eval_kb")
    return build_eval_kb(corpus, tmp)


@pytest.fixture(scope="session")
def eval_llm() -> LLMClient:
    """用 .env.test 的 LLM 配置建 LLMClient（检索阶段 transform / 生成阶段作答共用）。

    无 key 时 skip：本 fixture 只被集成门禁请求，不影响 test_harness 廉价单测。
    """
    env = _read_env_test()
    if not env.get("LANGCHAIN_API_KEY"):
        pytest.skip("无 LANGCHAIN_API_KEY（.env.test）—— 跳过 RAG 集成门禁")
    settings = Settings(
        agent_provider=env.get("LANGCHAIN_AGENT_PROVIDER", "openai"),
        agent_api_key=env.get("LANGCHAIN_API_KEY"),
        agent_base_url=env.get("LANGCHAIN_BASE_URL") or None,
        agent_model=env.get("LANGCHAIN_MODEL", "gpt-4.1-mini"),
    )
    return LLMClient(settings)


@pytest.fixture(scope="session")
def judge_env():
    """为 DeepEval（litellm 后端）设置 OpenAI-compatible judge env，返回模型名。"""
    env = _read_env_test()
    if not env.get("LANGCHAIN_API_KEY"):
        pytest.skip("无 LANGCHAIN_API_KEY（.env.test）—— 跳过 RAG 集成门禁")
    os.environ["OPENAI_API_KEY"] = env["LANGCHAIN_API_KEY"]
    if env.get("LANGCHAIN_BASE_URL"):
        os.environ["OPENAI_API_BASE"] = env["LANGCHAIN_BASE_URL"]
    return env.get("LANGCHAIN_MODEL", "gpt-4.1-mini")
```

> **关键**：没有任何 autouse skip。`test_harness.py` 的廉价单测**永远运行**（CI 必跑）；只有集成门禁因请求上述 session fixture 才会在缺 key/数据时 skip。

- [ ] **Step 2: 写门禁测试（report-only 起步）**

`backend/tests/rag_eval/test_rag_gate.py`：

```python
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from porto_chatbot.models.enums import QueryTransformStrategy

from .metrics import aggregate, build_metrics, evaluate_case, judge
from .runner import run_rag

pytestmark = pytest.mark.integration

_REPORT = Path(__file__).resolve().parent / ".last_report.json"
_STRATEGY = QueryTransformStrategy(os.environ.get("RAG_EVAL_STRATEGY", "none"))


def test_rag_quality_gate(eval_kb, domainrag_data, eval_llm, judge_env):
    _corpus, goldens = domainrag_data
    model = f"openai/{judge_env}"  # litellm 模型串
    metrics = build_metrics(model)

    per_case: list[dict[str, float]] = []
    case_reports: list[dict] = []
    errored: list[str] = []
    for idx, g in enumerate(goldens):
        try:
            tc, result = run_rag(g, eval_kb, eval_llm, strategy=_STRATEGY)
            scores = evaluate_case(tc, metrics)
        except Exception as e:  # noqa: BLE001
            errored.append(f"case#{idx} {g.question[:30]}: {e}")
            continue
        per_case.append(scores)
        case_reports.append({"question": g.question, "scores": scores, "degraded": result.degraded})

    mean = aggregate(per_case)
    passed, detail = judge(mean)
    _REPORT.write_text(
        json.dumps(
            {"mean": mean, "detail": detail, "cases": case_reports, "errored": errored, "n": len(goldens)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # report-only 起步：默认只产出报告、不因低分 fail；errored 始终 fail（真故障）。
    report_only = os.environ.get("RAG_EVAL_REPORT_ONLY", "1") == "1"
    assert not errored, f"有 case 出错（真故障）：{errored}"
    if not report_only:
        assert passed, f"RAG 门禁未达标：{detail}"
```

> report-only 语义：`RAG_EVAL_REPORT_ONLY=1`（默认）→ 不因指标低分 fail，仅 errored 才 fail；`RAG_EVAL_REPORT_ONLY=0` → 启用硬阈值判定。（注意：不要用 `pytest.mark.xfail()` 在函数体内抑制——它是 marker 装饰器，不是运行期断言开关。）

- [ ] **Step 3: 运行（需 key + 数据集）**

Run: `make eval-test`
Expected: 跑完整批 golden，输出 `.last_report.json`；report-only 模式下不因低分 fail。

- [ ] **Step 4: ruff + 提交**

Run: `cd backend && ruff check tests/rag_eval/`

```bash
git add backend/tests/rag_eval/
git commit -m "feat(eval): session fixtures + RAG 集成门禁（report-only）"
```

---

## 基线与启用硬阈值（实现完成后）

1. `RAG_EVAL_REPORT_ONLY=1 make eval-test` → 读 `.last_report.json` 的 `mean`，记为基线。
2. 把 `metrics.THRESHOLDS` 各值定在 `基线 − 余量`（约 0.10–0.15）。
3. `RAG_EVAL_REPORT_ONLY=0 make eval-test` → 启用硬阈值回归门禁。

## Self-Review

**1. Spec coverage：**
- 全链路（检索+生成+judge）→ T3(run_rag) + T4(metrics) + T6(gate) ✅
- 6 指标（含 AnswerCorrectness）→ T4 build_metrics ✅
- DomainRAG + Makefile 下载 + gitignore → T1(Makefile/gitignore) + T5(fetch/loader) ✅
- 隔离 eval KB（独立 Settings/collection/LOCAL embed）→ T2 provision ✅
- `@pytest.mark.integration` + 无 key 跳过 → T6 conftest ✅
- 批量均值 + report-only → T4 aggregate/judge + T6 ✅
- harness 廉价单测（synth）→ test_harness.py 贯穿 T1–T5 ✅
- 不动现有代码 → 全部新增 + pyproject/Makefile/.gitignore ✅
- `[eval]` 可选依赖、aggregate 不依赖 deepeval → T4 延迟导入 ✅

**2. Placeholder scan：** T5 `load_domainrag` 的 `corpus_path`/`qa_path` 文件名是**显式标注的核对后改动点**（非模糊 TODO），其核心 `load_domainrag_from_records` 有合成单测覆盖。无其他 TBD。

**3. Type consistency：** `RagGolden`/`RagCorpus`/`CorpusDoc`/`EvalKb`/`run_rag`/`build_metrics`/`evaluate_case`/`aggregate`/`judge` 跨 Task 名称与签名一致。`retrieve_with_transform(query,strategy,store,settings,llm,top_k)` 与 `query_transform.py:73` 一致；`LLMClient.complete(system,user,...)` 与 `llm/client.py:138` 一致。
