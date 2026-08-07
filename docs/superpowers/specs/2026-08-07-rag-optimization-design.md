# RAG 检索优化配置化设计

> 状态：Draft（待 review）
> 日期：2026-08-07
> 范围：把 HyDE / Multi-Query / Decomposition / Step-Back / Adaptive Routing 接入 Porto，做成 chat 和 workflow 的可选配置

## 背景与动机

Porto 当前的检索管线已经很成熟——hybrid 检索（向量+BM25 RRF 融合）+ LLMRerank 二次精排 + intent 路由（direct/rag）。但 query 从入口到 embedding **零变换**：`vector_store.py:197` 直接对原始 query embed。

2026 年 RAG 优化的主流方向（HyDE、Query Transformation、Adaptive Routing）能显著提升检索质量，且 Porto 已依赖的 llama-index / langgraph 原生支持，无需引入新依赖。本设计把这些算法做成 **chat 和 workflow 各自可选、可配置** 的能力，从算法接入到前端 UI 全链路支持。

关键现状发现：
- `QueryFusionRetriever`（`retrieval.py:115`）已支持 multi-query，但 `num_queries=1`（`retrieval.py:121`）被显式关闭——脚手架已在，只需配置化打开。
- `route_chat_intent`（`intent.py:35`）已是 adaptive router 雏形，但只 direct/rag 两路。
- chat query 是短问题（适合 HyDE）；workflow query 是 `prd_text[:2000]` 长文档（适合 decomposition，不适合 HyDE）。

## 目标

- 5 个 query transform 策略 + 3 个 routing 模式，全部可配、生效、可降级
- chat 和 workflow **场景分离**，各自独立配置 + 出厂默认值
- 前端横向卡片选择器，配置持久化
- 默认配置下**行为零变化**（向后兼容硬指标）

## 非目标（YAGNI）

| 不做 | 理由 |
|---|---|
| HyDE 自适应触发（低置信度才 fallback） | 需要置信度阈值 + 两次检索，复杂度高。`hyde_fallback_threshold` 字段占位但不实现逻辑 |
| routing 方案 C（每分支独立绑 transform） | 组合爆炸 |
| RAG-Fusion（hyde + multi_query 同时） | 需要时加 `strategy: "rag_fusion"` 枚举值 |
| 检索质量评测 dashboard | 独立工作，本次不捆绑 |
| workflow 的 routing | 固定流程无分叉 |
| multi_query prompt 前端自定义 | prompt 硬编码（中文友好） |
| embedding LOCAL / spec 模板等现有降级路径清理 | 超出本次范围 |

## 设计决策记录

brainstorming 过程中确认的关键选择：

| 决策点 | 选择 | 备选（未选） |
|---|---|---|
| 配置粒度 | chat/workflow 场景分离，各自默认 | ~~统一配置~~ / ~~全局默认+场景覆盖~~ |
| query_transform 配置模型 | 互斥策略 enum（横向卡片单选） | ~~多个独立 bool 开关~~（组合语义混乱） |
| routing 配置模型 | 模式 enum（off/binary/adaptive） | ~~仅开关~~ / ~~模式+每分支绑 transform~~ |
| 默认值 | chat: binary+none；workflow: none（全保守） | ~~adaptive+none~~ / ~~全开~~ |
| 卡片标题 | 英文技术名 | ~~中文翻译~~ |
| 降级哲学 | LLM 硬依赖 + 偶发失败容错 + 可见降级 | ~~无 key 完整功能替代~~（已废弃） |

## 架构：两个正交维度，场景分离

```
chat 入口 (langchain_chat.py)
  ├─ [intent_routing_mode] ── 决定"走哪条路"
  │     off      → 全走 RAG
  │     binary   → direct / rag（现状）
  │     adaptive → direct / quick_rag / deep_rag
  │                  └─ deep_rag 自动套用本场景的 query_transform
  └─ [query_transform_strategy] ── 决定"检索前怎么改写"
        none / hyde / multi_query / decomposition / step_back

workflow retrieve 节点 (agent/nodes/retrieve.py)
  └─ [query_transform_strategy] ── 仅 transform，无 routing
        none / multi_query / decomposition
```

routing 和 query_transform 解耦：
- **routing** 决定"要不要检索、走哪级"
- **query_transform** 决定"检索前怎么改写"
- 两者都是横向卡片单选，UI 统一

routing 仅作用于 chat（workflow 是固定流程 `retrieve → understand → identify → generate → evaluate`，retrieve 节点 query 确定，无分叉）。

## 配置数据模型

### 新增 namespace

新增 `rag_chat` / `rag_workflow` 两个 config_store namespace（而非往现有 `rag` namespace 塞带前缀字段）。现有 `rag` namespace（`retrieval_method`/`rerank_*`/`top_k` 等基础检索配置）chat/workflow 共享，保持不动。

```python
# config_store.py 新增
RAG_CHAT_SETTING_KEYS = {
    "intent_routing_mode",          # off | binary | adaptive
    "query_transform_strategy",     # none | hyde | multi_query | decomposition | step_back
    "multi_query_count",            # multi_query 时生成几个改写（默认 4）
    "hyde_fallback_threshold",      # 占位（自适应触发，本次不实现逻辑）
}
RAG_WORKFLOW_SETTING_KEYS = {
    "query_transform_strategy",     # none | multi_query | decomposition
    "multi_query_count",
}
```

### 新增枚举（models/enums.py）

```python
class IntentRoutingMode(StrEnum):
    OFF = "off"
    BINARY = "binary"
    ADAPTIVE = "adaptive"

class QueryTransformStrategy(StrEnum):
    NONE = "none"
    HYDE = "hyde"
    MULTI_QUERY = "multi_query"
    DECOMPOSITION = "decomposition"
    STEP_BACK = "step_back"

# ChatIntent 扩展（新增 quick_rag / deep_rag）
class ChatIntent(StrEnum):
    DIRECT = "direct"
    RAG = "rag"               # binary 模式用
    QUICK_RAG = "quick_rag"   # adaptive 模式：快速检索（强制 none）
    DEEP_RAG = "deep_rag"     # adaptive 模式：深度检索（套 transform）
```

### settings.py 新字段（默认值=方案1，全保守）

```python
# --- RAG 检索优化（场景分离）---
chat_intent_routing_mode: IntentRoutingMode = IntentRoutingMode.BINARY
chat_query_transform_strategy: QueryTransformStrategy = QueryTransformStrategy.NONE
workflow_query_transform_strategy: QueryTransformStrategy = QueryTransformStrategy.NONE
multi_query_count: int = Field(default=4, ge=2, le=8)
hyde_fallback_threshold: float = Field(default=0.3, ge=0.0, le=1.0)  # 占位，本次不实现逻辑
```

### API 全链路（同现有模式）

- `models/`：新增 `RagChatSettingsPayload` / `RagWorkflowSettingsPayload`，`AppSettingsResponse` 加 `rag_chat` / `rag_workflow` 子对象
- `api/deps.py`：新增 `effective_rag_chat_settings()` / `effective_rag_workflow_settings()`（合并环境默认 + DB 覆盖）
- `api/routes/settings.py`：GET/PUT 处理两个新子对象

## 算法接入

### 设计原则

**LLM 调用全走 `LLMClient`（降级统一），检索编排用 llama-index**。不依赖 llama-index 的 `HyDEQueryTransform` 等 LLM 抽象——prompt 不可控、中文效果差。所有 `_generate_*` 用 `LLMClient.complete` + 结构化解析。

### transform 的本质：编排检索，不是只改字符串

5 策略对下游影响分三类：

| 类型 | 策略 | 行为 |
|---|---|---|
| 不变换 | none | 原样检索（现状） |
| 单 query 变换 | hyde / step_back | LLM 生成假答案/抽象问题 → 对变换后 query 单次检索 |
| 多 query 融合 | multi_query | LLM 生成 N 改写 → 各自检索 → RRF 融合 |
| 子问题拆分 | decomposition | LLM 拆 M 子问题 → 各自检索 → 合并去重 |

新建 `query_transform.py` 封装"transform + 检索"完整编排，返回 `TransformResult`。

### vector_store.py 重构

拆出无 rerank 的基础检索：

```python
def _search_raw(self, query, top_k) -> list[SourceChunk]:
    """基础检索（vector/bm25/hybrid），不含 rerank。供 query_transform 编排。"""
    # 现有 search() 里 embed + method 分发那一段

def search(self, query, top_k) -> list[SourceChunk]:  # 现状入口，保持
    rows = self._search_raw(query, top_k)
    if self.settings.rerank_enabled: rows = rerank_chunks(rows, query, ...)
    return rows
```

rerank 提升为 transform 编排的最后一步——对所有候选统一精排。

### query_transform.py 接口

```python
@dataclass
class TransformResult:
    chunks: list[SourceChunk]
    degraded: bool = False        # 是否发生降级
    degrade_reason: str = ""      # 降级原因

def retrieve_with_transform(
    query, strategy, store, settings, llm, top_k,
) -> TransformResult:
    if strategy == QueryTransformStrategy.NONE:
        return TransformResult(store.search(query, top_k))   # 现状路径零改动
    try:
        if strategy == HYDE:
            fake_doc = _generate_hypothetical(llm, query)
            rows = store._search_raw(fake_doc, top_k)
        elif strategy == MULTI_QUERY:
            variants = _generate_query_variants(llm, query, settings.multi_query_count)
            rows = _rrf_fuse([store._search_raw(v, top_k) for v in variants])
        elif strategy == DECOMPOSITION:
            sub_qs = _decompose(llm, query)
            rows = _merge_dedupe([store._search_raw(s, top_k) for s in sub_qs])
        elif strategy == STEP_BACK:
            abstract = _step_back(llm, query)
            rows = store._search_raw(abstract, top_k)
    except Exception:
        logger.exception("transform failed strategy=%s → fallback raw", strategy)
        rows = store._search_raw(query, top_k)
        return TransformResult(rows, degraded=True, degrade_reason="llm_call_failed")
    if settings.rerank_enabled:
        rows = rerank_chunks(rows, query, settings)
    return TransformResult(rows)
```

- `_rrf_fuse`：手写 RRF（`score = Σ 1/(rank+k)`，k=60），或调 llama-index `reciprocal_rank_fusion`
- 嵌套融合语义：`multi_query` + `retrieval_method=hybrid` 产生两层 RRF（每个改写先向量+BM25 融合，再跨 query RRF），这是正确的"多角度提问 × 双路召回"叠加

### routing × transform 协同语义

transform 和 routing 解耦——**只要走检索就读 query_transform_strategy**，routing 只管分流：

| routing_mode | 分流 | 检索时是否套 transform |
|---|---|---|
| off | 全部检索 | 套（按 strategy） |
| binary | direct（不检索）/ rag | rag 套（按 strategy） |
| adaptive | direct / quick_rag / deep_rag | quick_rag **强制 none**；deep_rag 套（按 strategy） |

向后兼容：默认 `binary`+`none` → rag 分支套 none = 现状 `store.search(query)`，行为零变化。

### intent.py 扩展

`route_chat_intent` 接受 `routing_mode` 参数：
- `off`：**不调用** `route_chat_intent`——`langchain_chat` 直接走检索（套 transform）。off 语义是"不分类、所有 query 都检索"，intent 函数此模式下不被调用
- `binary`：产出 direct/rag（现状）
- `adaptive`：LLM 分类器 prompt 枚举扩到 direct/quick_rag/deep_rag；规则降级（`_rule_route`）同步加 quick_rag/deep_rag 关键词判断

### 接入点（仅两处）

```python
# langchain_chat.py（chat）
if chat_routing_mode == OFF:
    result = retrieve_with_transform(message, chat_strategy, store, settings, llm, top_k)  # 不分类，直接检索
    sources = result.chunks
else:
    intent = route_chat_intent(message, ..., routing_mode=chat_routing_mode)
    if intent == DIRECT: → 不检索
    elif intent == QUICK_RAG: sources = store.search(message, top_k)
    else:  # RAG / DEEP_RAG
        result = retrieve_with_transform(message, chat_strategy, store, settings, llm, top_k)
        sources = result.chunks
if result.degraded: response.transform_degraded = result.degrade_reason  # 统一降级标记

# agent/nodes/retrieve.py（workflow）
result = retrieve_with_transform(query, workflow_strategy, store, settings, llm, top_k)
sources = result.chunks
# degraded 通过 retrieve 节点 _step 的 metadata dict 传递（加 transform_degraded 键），无需改 AgentStep 模型
```

## 降级策略（重新规划）

### 前提变化

Porto 已从"无 key 也能跑"成长为"LLM 是硬依赖的专业工具"。能用 Porto = LLM key 已配。区分两个概念：
- **无 key 功能替代**（旧哲学）→ 废弃
- **偶发失败容错**（生产工程）→ 保留

### 降级矩阵

| 场景 | 行为 |
|---|---|
| 策略需要 LLM + LLM 可用 | 正常执行 transform |
| 策略需要 LLM + LLM 偶发失败（超时/限流/网络） | fail-open 回退 `_search_raw(query)` + `degraded=True` + 日志 |
| 策略需要 LLM + 根本没配 key | 不出现（没 key 进不来 Porto） |

### 可见降级

偶发失败时**不静默**——`TransformResult.degraded` 标记传到 `ChatResponse.transform_degraded` / `AgentStep` 元数据，前端可展示"⚠️ 查询变换未生效，已使用基础检索"。用户主动配了策略，有权知道有没有生效。

intent routing 的 LLM 分类器偶发失败 → 退回 `_rule_route`（定位从"无 key 替代"改为"偶发失败兜底"）。

## 前端 UI

### 新增「检索优化」tab

`SettingsSection` 新增 `"retrieval_optimization"`。不塞进现有 rag tab（已拥挤）。

```
检索优化
├─ Chat 场景
│   ├─ 意图路由模式     [Off] [Binary] [Adaptive]     ← 横向卡片三选一
│   └─ 查询变换策略     [None][HyDE][Multi-Query][Decomposition][Step-Back]
│       └─（选中 Multi-Query 时展开）改写数量：[▬▬▬○▬] 4
└─ Workflow 场景
    └─ 查询变换策略     [None][Multi-Query][Decomposition]   ← 三选一（无 HyDE/Step-Back）
        └─（选中 Multi-Query 时展开）改写数量滑块
```

tab 顶部提示：「查询优化为查询时行为，修改后立即生效，无需重建知识库索引。」

### StrategyCardGroup 组件（可复用）

props: `{ options: {value, label, description}[], value, onChange }`

卡片样式：标题（英文，粗体）+ 一句话介绍（中文，次级色，2 行内）+ 选中态高亮边框/背景。

### 卡片文案（英文标题 + 中文介绍）

意图路由模式（chat）：

| 卡片 | 标题 | 介绍 |
|---|---|---|
| off | Off | 不做意图分流，所有消息都查知识库 |
| binary | Binary | 自动区分闲聊与知识库问答；闲聊直答，其余查库 |
| adaptive | Adaptive | 三级分流——闲聊直答 / 快速检索 / 深度检索（自动套用查询变换） |

查询变换策略：

| 卡片 | 标题 | 介绍 |
|---|---|---|
| none | None | 直接用原始问题检索 |
| hyde | HyDE | 先生成假设性答案再检索，弥补问题与文档的措辞差异（+1 次模型调用） |
| multi_query | Multi-Query | 生成多个改写问题分别检索后融合，提升召回 |
| decomposition | Decomposition | 将复杂问题拆成子问题分别检索，适合多跳追问 |
| step_back | Step-Back | 先抽象出更高层问题，检索背景知识（仅 chat） |

介绍里标注代价（如"+1 次模型调用"）——呼应"花钱的默认关"的透明原则。

### 数据流

- 前端拆 `RagChatSettingsForm` / `RagWorkflowSettingsForm`（各一组 `StrategyCardGroup`）
- `AppSettings` 类型新增 `rag_chat` / `rag_workflow`
- `lib/api.ts` 透传两个新子对象

## 测试策略

| 新增/扩展 | 覆盖 |
|---|---|
| `test_query_transform.py`（新） | 5 策略单测：mock LLM 验证 hyde embed 的是假答案、multi_query 验证 RRF 融合、decomposition 验证合并去重；降级路径：LLM 抛异常 → 回退 `_search_raw` + `degraded=True` |
| `test_intent.py`（扩展） | adaptive 三级分类 + 规则降级 + routing_mode 控制 |
| `test_config_store.py`（扩展） | `rag_chat`/`rag_workflow` 两 namespace 存取 |
| `test_settings_api.py` / `test_settings_backend.py`（扩展） | 新字段 GET/PUT 往返 |
| 现有测试 | 全过（默认值零行为变化硬保证） |

## 成功标准

1. 5 个 transform 策略 + 3 个 routing 模式全部可配、生效、可降级
2. **默认配置下现有测试 100% 通过**（向后兼容硬指标）
3. 前端横向卡片可交互、配置持久化到 SQLite、刷新不丢
4. LLM 可用时功能完整；LLM 偶发失败时 fail-open 不崩 + 降级可见（日志 + 响应标记）
5. 改配置立即生效（无需重建索引）

## 实现顺序建议（供 writing-plans 参考）

1. 后端配置基建：enums + settings + config_store namespace + models payload + api/deps + api/routes
2. `query_transform.py` 算法核心 + vector_store `_search_raw` 重构
3. `intent.py` 扩展（routing_mode + 三级分类）
4. 接入点：langchain_chat + retrieve 节点 + transform_degraded 传递
5. 前端：types + api + StrategyCardGroup + 两个表单 + tab
6. 测试：test_query_transform + 扩展 intent/config_store/settings_api

## 涉及文件清单

**后端：**
- `models/enums.py`（新枚举 + ChatIntent 扩展）
- `settings.py`（新字段）
- `config_store.py`（两 namespace + KEYS）
- `models/`（两 payload + AppSettingsResponse 扩展 + ChatResponse 加 `transform_degraded` 字段）
- `api/deps.py`（两 effective_* 函数）
- `api/routes/settings.py`（GET/PUT 扩展）
- `query_transform.py`（新模块）
- `vector_store.py`（拆 _search_raw）
- `retrieval.py`（`_rrf_fuse` 工具，可选）
- `intent.py`（routing_mode + 三级分类）
- `agent/langchain_chat.py`（接入 + degraded 传递）
- `agent/nodes/retrieve.py`（接入）

**前端：**
- `lib/types.ts`（AppSettings 扩展）
- `lib/api.ts`（透传）
- `components/porto-workbench.tsx`（新 tab + 两表单 + StrategyCardGroup）

**测试：**
- `tests/test_query_transform.py`（新）
- `tests/test_intent.py`（扩展）
- `tests/test_config_store.py`（扩展）
- `tests/test_settings_api.py` / `test_settings_backend.py`（扩展）
