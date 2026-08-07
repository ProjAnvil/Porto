# RAG 质量评测门禁设计（DeepEval + DomainRAG）

> 状态：Draft（待 review）
> 日期：2026-08-07
> 范围：为 Porto 的 RAG 管线建立一套基于 DeepEval 的 LLM-as-judge 质量回归门禁，以公开中文数据集 DomainRAG 作为可复现任置语料，全链路（检索 + 生成）评测，集成进 pytest 体系。

## 背景与动机

Porto 已有一套 RAG 评测设施，但定位是「轻量、本地、免费」的启发式代理：

- `backend/src/porto_chatbot/evaluation.py` → `evaluate_rag_case(s)` 计算加权分，四个 RAGAS 风格维度：
  - `answer_relevance` / `context_relevance`（本地 embedding + 余弦相似度）
  - `groundedness`（词表重叠）
  - `faithfulness`（句子支持度启发式）
- 代码注释自述 *"Lightweight local eval inspired by RAGAS dimensions; no judge LLM required."*
- 经 `/api/eval/rag`（`api/routes/eval.py`）与 `tests/test_memory_eval.py` 暴露。

这是「快且免费」的代理指标，**不是真正的 LLM-as-judge 评测**。本设计补上「深而准」的一层：用 DeepEval 的标准 RAG 指标（faithfulness / answer relevancy / contextual precision-recall-relevancy）做回归门禁。

与此同时，`2026-08-07-rag-optimization-design.md`（其核心 `query_transform.py` / `retrieve_with_transform` / `ChatResponse.transform_degraded` 已落地）引入了 5 个 query-transform 策略 + 3 个 routing 模式，正是最需要回归保护的变更面。该 spec 显式把「检索质量评测 dashboard」作为独立工作搁置——本设计正是这个独立工作，但**范围收窄为回归门禁（pytest），不含 API/UI dashboard**。

## 目标

- 一套 pytest 集成的 DeepEval RAG 质量门禁，全链路（检索 + 生成 + judge）。
- 以公开、可自由下载的**中文**数据集 DomainRAG 作为评测语料，**无需用户提供数据**。
- 数据集经 Makefile 脚本下载、gitignore，可复现、不进 git。
- `@pytest.mark.integration`：无 LLM key 自动跳过，与现有 marker 模式一致。
- 直接覆盖 `retrieve_with_transform` 的检索面 + `LLMClient.complete` 的生成面，追踪 RAG 优化迭代的回归。

## 非目标（YAGNI）

| 不做 | 理由 |
|---|---|
| 替换/改动现有 `evaluation.py` + `/api/eval/rag` | 那是运行时免费启发式层，与本次 LLM-judge 层各司其职，互不替代 |
| 评测 dashboard / API / UI | 按 RAG 优化 spec 继续搁置；本次纯测试 |
| 多数据集 / 数据集切换 UI | 先交付 DomainRAG 一个；loader 接口归一化即可，后续加别的零成本 |
| 走 `langchain_chat` 全编排 | 含意图路由/记忆/session 噪声，且耦合 store 单例；门禁直接编排「检索+生成」核心更精准 |
| 每 case 硬阈值逐条判定 | LLM-judge 抖动大；改为批量均值判定 |
| free IR 指标（nDCG/Recall@k） | 数据集 qrels 字段未确认；本次聚焦 deepeval LLM-judge 全套。后续可加确定性锚点 |

## 设计决策记录

brainstorming 过程中确认的关键选择：

| 决策点 | 选择 | 备选（未选） |
|---|---|---|
| 评测目的 | A 回归质量门禁 | ~~B 一次性离线测量~~ / ~~C 替换现有 eval~~ / ~~D 探索~~ |
| 数据集语料 | A 中文数据集（DomainRAG） | ~~英文 NanoBEIR/SciFact~~ |
| 具体数据集 | DomainRAG（开放下载） | ~~SuperCLUE-RAG（需邮件申请，不满足可复现/CI）~~ / ~~CMRC2018（偏阅读理解）~~ |
| 管线深度 | ① 全链路（检索+生成+judge） | ~~分层（free IR + deepeval 两层）~~ |
| harness 结构 | A pytest-native、进程内 | ~~B 独立 CLI/HTTP runner~~ |
| 检索/生成入口 | 直接编排 `retrieve_with_transform` + `LLMClient.complete` | ~~`langchain_chat` 全编排~~ |
| 数据集供给 | Makefile 脚本下载 + gitignore | ~~vendor 进 repo~~ |
| 代码位置 | `tests/rag_eval/`（不进 `src/`） | ~~进 src 做成可复用模块~~ |
| 依赖归属 | `[project.optional-dependencies] eval` | ~~进 runtime deps~~ |
| embedding | LOCAL（确定性、零成本） | ~~镜像生产 embedding~~ |
| 阈值判定 | 批量均值 + report-only 起步 | ~~逐条硬阈值~~ |

## 架构

**核心判断**：门禁直接编排 RAG 核心（检索 + 生成），而非走 `langchain_chat` 全编排。后者含意图路由 / 记忆 / session 状态，会给「检索质量门禁」引入噪声，并耦合可能的 store 单例。`retrieve_with_transform` + `LLMClient.complete` 本身即完整的「检索→生成」链路，且正好直接覆盖 query-transform 策略这一最需要回归保护的变更面。

```
tests/rag_eval/data/domainrag/         # gitignored，make eval-dataset 下载落地
  └─ loaders/domainrag ─→ (RagCorpus, [RagGolden])          [session 级，一次]
       └─ provision.build_eval_kb():
            corpus → tmp kb_dir 落成文件
            隔离 Settings(collection="eval_domainrag", chroma_dir=tmp,
                          kb_dirs=[tmp], embedding=LOCAL)
            ChromaVectorStore.build() + BM25                → EvalKb(store, settings)
  对每条 RagGolden(parametrize):
       retrieve_with_transform(q, strategy, store, settings, llm, top_k) → sources
       llm.complete(rag_answer_prompt(q, sources))          → answer
       LLMTestCase(input=q, actual_output=answer,
                   expected_output=golden.reference_answer,
                   retrieval_context=[s.text for s in sources])
       ── deepeval metrics(judge = Porto 的 OpenAI-compatible LLM) ──
  └─ 批量聚合 vs 阈值 → pass/fail + tests/rag_eval/.last_report.json
```

## 数据集与供给

**数据集：DomainRAG**（[GitHub: ShootingWong/DomainRAG](https://github.com/ShootingWong/DomainRAG)，[arXiv:2406.05654](https://arxiv.org/abs/2406.05654)，被引 48+）

- 中文、领域（高校招生）、**专为领域 RAG 评测设计**，覆盖抽取式 / 对话式 / 多文档 / 时效等子集。
- 语料 + QA 经 Google Drive 公开下载，**无需申请**（满足可复现 / CI 友好）。
- 存在 "golden-references" 评测设置 → 暗示每条 query 带金标段落（contextual recall 可算）；确切字段名实现时下载核对。

**为什么不 vendor 进 git**：语料体量不小，进 git 污染历史、不可复现更新。改为：

- `tests/rag_eval/scripts/fetch_dataset.py`：用 `gdown` 拉 Google Drive 的 `corpus.tar.gz` + QA 集，校验、解压到 `tests/rag_eval/data/domainrag/`。
- Makefile 新增 `eval-dataset` target 调用该脚本；`eval-test` target 跑门禁。
- `.gitignore` 新增 `backend/tests/rag_eval/data/`。
- loader 读 `data/domainrag/`；缺失时 `pytest.skip` 并提示 `make eval-dataset`。

**领域差异说明**：DomainRAG 是「高校招生」领域，与 Porto 的 PRD / 技术文档不同域。但作为**回归门禁**，「可复现 + 跑通中文检索/生成全链路 + 有金标」比同域更重要——门禁测的是「改动有没有让质量回归」，而非「领域准确率」。

## 组件

全部位于 `backend/tests/rag_eval/`，**不进 `src/`**：

| 文件 | 职责 |
|---|---|
| `schema.py` | 归一化 schema：`RagGolden(question, reference_answer, gold_doc_ids, category)`、`RagCorpus(docs=list[CorpusDoc(id, text)])` |
| `loaders/domainrag.py` | `load_domainrag() -> (RagCorpus, [RagGolden])`：读 `data/domainrag/`，按 DomainRAG 真实字段映射成归一化 schema |
| `scripts/fetch_dataset.py` | 可执行下载脚本：`gdown` 拉取 + 解压；Makefile 调用 |
| `provision.py` | `build_eval_kb(corpus, settings_template) -> EvalKb`：写 tmp kb_dir → 隔离 Settings → `ChromaVectorStore.build()`。session 级 |
| `runner.py` | `run_rag(golden, eval_kb, llm) -> LLMTestCase`：检索 → 生成 → 组装测试用例 |
| `metrics.py` | DeepEval 指标集 + `THRESHOLDS` + 批量聚合器 `aggregate()` |
| `conftest.py` | session fixtures：`eval_kb` / `goldens` / `llm` / deepeval judge 配置 |
| `test_rag_gate.py` | parametrize 的门禁本体，`@pytest.mark.integration` |
| `test_harness.py` | harness 自身的廉价单测（无 LLM，CI 必跑） |
| `synth/` | committed 的迷你合成语料（3~5 篇 + 2~3 条 golden），供 `test_harness.py` |
| `data/` | **gitignored**，DomainRAG 下载落地处 |

## 数据流

1. `fetch_dataset.py`（手动 / `make eval-dataset`）→ `data/domainrag/`。
2. session fixture：`load_domainrag()` → `(RagCorpus, [RagGolden])`。
3. session fixture：`build_eval_kb(corpus)` → corpus 落 tmp kb_dir 文件 → 隔离 Settings（`vector_collection="eval_domainrag"`、`embedding_provider=LOCAL`、`chroma_dir`/`kb_dirs` 指向 tmp）→ `ChromaVectorStore.build()` + BM25 → `EvalKb(store, settings, llm)`。
4. 每条 `RagGolden`（parametrize）：
   - `retrieve_with_transform(q, strategy, store, settings, llm, top_k)` → `sources: list[SourceChunk]`
   - `llm.complete(rag_system_prompt, user=f"{q}\n\n上下文:\n{sources}")` → `answer`（None 时 guard）
   - 组 `LLMTestCase(input=q, actual_output=answer, expected_output=reference_answer, retrieval_context=[s.text for s in sources])`
5. DeepEval 6 指标评测（judge = Porto 的 OpenAI-compatible LLM）。
6. `aggregate()` → 各指标批量均值 vs `THRESHOLDS` → pass/fail，写 `.last_report.json`。

## 指标集

DeepEval 2026 标准 RAG 套件（全 LLM-as-judge），6 个指标：

| 指标 | 测什么 | 所需 LLMTestCase 字段 |
|---|---|---|
| `FaithfulnessMetric` | 答案是否忠于召回上下文（不臆造/覆盖幻觉） | actual_output + retrieval_context |
| `AnswerRelevancyMetric` | 答案是否切题 | input + actual_output |
| `AnswerCorrectnessMetric` | 答案相对**参考答案**的事实正确性 | input + actual_output + expected_output |
| `ContextualPrecisionMetric` | 相关 chunk 是否排在前面（rerank 质量） | input + retrieval_context + expected_output |
| `ContextualRecallMetric` | 该召回的是否都召回了 | expected_output + retrieval_context |
| `ContextualRelevancyMetric` | 召回上下文是否与问题相关 | input + retrieval_context |

**为什么含 AnswerCorrectness**：DomainRAG 带参考答案（`expected_output`），AnswerCorrectness 是唯一"拿参考答案判事实正确性"的指标，与 Faithfulness（对上下文忠实）互补——一个查"有没有说错"，一个查"说得对不对"。社区/工业最佳实践（Patronus AI、DeepLearning.ai）都把 context precision/recall + answer correctness 列为重中之重。

**暂不做**：`GEval`（自定义 CoT LLM-judge，DeepEval 最热门指标）留作日后自定义质量维度的钩子；`HallucinationMetric` 与 Faithfulness 重叠，不重复。

## 阈值与抖动策略

LLM-as-judge 单条抖动大，门禁可用性的关键在降噪：

- **批量均值判定**：每指标取全体 golden 的均值 vs 阈值，而非逐条 pass（平滑单条噪声）。
- **report-only 起步**：`RAG_EVAL_REPORT_ONLY=1` 时只打印 + 写 `.last_report.json`、不 assert。先跑基线分，再把阈值定在**基线留余量**处（如基线 0.70 → 阈值 0.55）。
- 阈值集中在 `metrics.py` 的 `THRESHOLDS` dict（可调），默认偏松防 flaky。

## 降级与错误处理

| 场景 | 行为 |
|---|---|
| 数据集缺失（未 `make eval-dataset`） | `pytest.skip("run: make eval-dataset")`，不报错 |
| KB build 失败 | skip + 清晰错误信息 |
| 某 golden 检索召回 0 条 | 照常建 `LLMTestCase`（空 retrieval_context）；faithfulness/contextual 自然低分——**真实回归信号**，非崩溃 |
| 生成或 judge 的 LLM 调用失败 | 该 case 标 `errored`；**默认有 errored 即整批 fail**（真故障要响），可配置放行 |

## 测试策略（harness 自身）

门禁本体是 `integration`，但 harness 的装配逻辑必须有廉价单测保护：

| 测试 | 覆盖 | 运行条件 |
|---|---|---|
| `test_harness.py` | 用 `synth/` 合成语料单测：loader 归一化、`build_eval_kb` 建库后 `store.search` 命中已知文档、`run_rag` 字段组装（mock 掉 retrieve + `llm.complete`） | CI 必跑，无 LLM |
| `test_rag_gate.py` | 真 DomainRAG + 真 LLM 的门禁本体，5 指标批量均值判定 | `@pytest.mark.integration`，无 key 跳过 |

## 依赖与集成

- `backend/pyproject.toml` 新增可选依赖组：

  ```toml
  [project.optional-dependencies]
  document-ai = ["docling>=2.0.0"]
  eval = ["deepeval>=2.0.0", "gdown>=2.0.0"]
  ```

- 安装：`pip install -e ".[eval]"`（可加 `eval-install` make target）。
- 现有 `integration` marker 复用（`pyproject.toml` 已定义）。
- judge LLM：DeepEval 配置为 OpenAI-compatible，复用 Porto `settings` 的 `llm_base_url` / key / model。

## 边界

- **不动** `evaluation.py` + `/api/eval/rag`（运行时免费启发式层，原样保留）。
- 新门禁**纯测试、无 API/UI**（dashboard 按 RAG 优化 spec 继续搁置）。
- `deepeval` / `gdown` 只进 `[project.optional-dependencies] eval`，不污染生产依赖。
- 与 `evaluation.py` 共享「RAG 评测」概念但不共享代码；未来可共享 golden，本次 YAGNI。

## 成功标准

1. `make eval-dataset` 可重复下载 DomainRAG 到 gitignored 目录；`data/` 不进 git。
2. `test_harness.py`（合成语料）CI 全绿、无需 LLM。
3. `test_rag_gate.py` 在配 key 环境下能跑完整批 DomainRAG golden，输出 6 指标均值 + `.last_report.json`。
4. report-only 模式可产出基线分；切换为硬阈值后，批量均值判定生效。
5. 现有测试不受影响（新代码全在 `tests/rag_eval/` + 可选依赖，零侵入）。

## 实现顺序建议（供 writing-plans 参考）

1. 依赖与脚手架：`pyproject.toml` 加 `[eval]`；`tests/rag_eval/` 骨架 + `schema.py` + `synth/` 合成语料。
2. `fetch_dataset.py` + Makefile `eval-dataset` / `eval-test` + `.gitignore`。
3. `loaders/domainrag.py`（下载后核对字段再定映射）。
4. `provision.py`（隔离 KB build）+ `test_harness.py` 装配单测先行。
5. `runner.py`（`retrieve_with_transform` + `llm.complete`）+ `metrics.py`（指标集 + 聚合）。
6. `conftest.py` session fixtures + `test_rag_gate.py`（report-only 起步）。
7. 跑基线 → 定 `THRESHOLDS` → 切硬阈值。

## 涉及文件清单

**新增（全部 `backend/tests/rag_eval/`）：**
- `schema.py`、`metrics.py`、`provision.py`、`runner.py`
- `loaders/__init__.py`、`loaders/domainrag.py`
- `scripts/fetch_dataset.py`
- `conftest.py`、`test_rag_gate.py`、`test_harness.py`
- `synth/`（迷你合成语料，committed）
- `data/`（gitignored，运行时落地）

**改动：**
- `backend/pyproject.toml`（`[eval]` 可选依赖）
- `Makefile`（`eval-dataset` / `eval-test` target）
- `.gitignore`（`backend/tests/rag_eval/data/`）

**不动：** `evaluation.py`、`api/routes/eval.py`、`tests/test_memory_eval.py` 及其余现有代码。

## 待实现时核实项

- DomainRAG 下载后的确切字段名（corpus 文档结构、QA 集 `query` / 参考答案 / golden-references 字段），据此定 `loaders/domainrag.py` 的映射。
- Google Drive 文件 id（README 所示 `1NquEyPGwP0MpTGJwDUUYKU37snYN4Er4`）有效性 / 是否拆多文件，据此调 `fetch_dataset.py`。

## 已核实项（spec review 阶段确认）

- `retrieve_with_transform(query, strategy, store, settings, llm, top_k) -> TransformResult`（`query_transform.py:73`）：签名如上；`store` 显式参数 → **无全局单例耦合**，可传入隔离 store 实例。
- `store.search` / `_search_raw` / BM25 全部走 `self.settings`；`build()` 会落 eval 专属 BM25 → 隔离 Settings 自洽。
- `LLMClient.complete(system, user, *, messages=None) -> str | None`（`llm/client.py:138`）：**两参**（system, user）形态，返回 `str | None`（禁用时 None）。runner 的生成调用须写成 `llm.complete(rag_system_prompt, user=f"{question}\n\n上下文:\n{sources}")` 并对 None 做 guard。
- DeepEval 2026 标准 RAG 指标 = 上述 6 个；社区最常用排序 GEval > AnswerRelevancy > Faithfulness > ContextualPrecision > ContextualRecall（[DeepEval 指南](https://deepeval.com/guides/guides-rag-evaluation)、[Confident AI](https://www.confident-ai.com/blog/rag-evaluation-metrics-answer-relevancy-faithfulness-and-more)、[Patronus AI](https://www.patronus.ai/llm-testing/rag-evaluation-metrics)、[DeepLearning.ai](https://community.deeplearning.ai/t/rag-evaluation-metrics-score-threshold-and-when-to-use-each-metric/742660)）。
