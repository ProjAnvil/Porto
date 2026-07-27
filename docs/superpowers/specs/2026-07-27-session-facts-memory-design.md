# 会话级关键事实记忆(Session Facts)设计

> 日期：2026-07-27　　状态：待评审
> 触发：用户反馈"补充信息点但没说全会话时,agent 接不上上下文";对照 2026 RAG / context engineering 业界实践后,发现 Agent Memory 层缺 long-term key facts 这一档。

## 1. 背景与问题

Porto 有两层 memory + RAG:

| 层 | 文件 | 现状 |
|---|---|---|
| **A. KB RAG** | `vector_store.py` / `retrieval.py` / `bm25_index.py` / `documents.py` | Hybrid(vector+BM25)+ RRF + 可选 LLMRerank。已基本对齐 2026 主干,**本次不动** |
| **B. Agent Memory** | `memory/store.py` / `memory/compaction.py` | 单路 dense + session_id where 过滤;有 sliding window + compaction 摘要。**本次重点改造对象** |

### 用户痛点

> "agent 不知道我在说什么,需要我提供更多信息的时候,如果我补充了信息点,但是没有说全会话,就不知道我在说什么了。"

典型场景:
- Turn 1: 用户"做个登录页"
- Turn 2: agent"需要更多信息,登录方式?"
- Turn 3: 用户"用 OAuth 吧" —— 省略句,**query 残缺**

### 根因排查

代码路径(`api/routes/chat.py:155-168` 非流式 / `:283-309` 流式):
- `memory.search(req.message, session_id, top_k=5)` —— **用残缺的当前消息做 query 召回历史**,容易召回到不相关的 OAuth 片段或 agent 自己的提问
- `get_compacted_history` —— 已经实现了 sliding window + 摘要,对标 2026 业界共识(详见 §3)
- prompt 拼装顺序:用户问题 → 摘要 → 最近会话 → 记忆检索 → KB 片段

痛点主要发生在**短会话**(< `memory_compact_threshold=20` 条):此时根本不触发 summary,所有原文在 `recent` 里。理论上 LLM 应该能从 recent 里消解指代,但实际效果差——**根因是没有"已确认关键事实"这一档长期记忆把实体词钉死**。

### 业界对照(2026)

Tiered Memory 是 [Anthropic](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)、[Mem0](https://mem0.ai/blog/context-engineering-in-multi-turn-ai-agents)、[Towards AI](https://pub.towardsai.net/long-context-compaction-for-ai-agents-part-1-design-principles-2bf4a5748154) 收敛的共识:

| 层 | 作用 | Claude Code 做法 | Porto 现状 |
|---|---|---|---|
| Short-term | 最近 N 轮原文 | sliding window + tool result pruning | ✅ `keep_recent=8` |
| Mid-term | 旧消息摘要 | auto-compact,LLM 压缩 | ✅ `compact_threshold=20` + 缓存 |
| **Long-term / Key Facts** | 关键事实结构化 | **`CLAUDE.md` + auto memory** | ❌ **完全缺失** |

> *"Pure sliding window alone is now considered insufficient for production-grade long-running agents — retrieval and memory mutation become essential."* — Mem0

## 2. 目标 / 非目标

**目标**
- **a1 实体感知摘要**:强化 `summarize_records` prompt,强制保留专有名词/变量名/数字/已确认结论,缓解旧消息摘要抽象化问题
- **a2 会话级关键事实层**:新增 `session_facts` 结构化记忆,从对话中提取 user_decision / user_preference / project_context / open_question 四类事实,每轮异步提取、按优先级注入 system prompt
- **可观测**:暴露 API,前端能看到 agent 记住了什么
- **fail-open**:任何环节失败不阻塞主 chat 链路

**非目标**
- ❌ 不做 KB RAG 改造(已是主干,本次不动)
- ❌ 不做跨 session 长期记忆(用户明确仅当前会话内,范围 (a))
- ❌ 不做查询改写(query rewriting,之前讨论的 (b) 路线)——YAGNI,先看 a1+a2 效果
- ❌ 不引入 hybrid+rerank 到 Agent Memory(之前讨论的 (c) 路线)——YAGNI
- ❌ 不做增量摘要(之前讨论的 (a3))——YAGNI,收益不如 a2 直接

## 3. 方案选型记录

### 提取触发机制

讨论过四种,最终选 **δ-1 异步每轮 + LLM 自决**:

| 选项 | 否决理由 |
|---|---|
| α 每轮同步提取 | 阻塞响应,每轮 +1 LLM 调用,延迟成本不可接受 |
| β 跟 compact 同步触发(阈值) | 短会话完全不触发,而痛点恰恰在短会话 |
| γ 关键词规则预筛 | **"永远不知道有多少确认词"** —— 多语言、口语化、间接表达全漏,脆弱 |
| **δ 异步每轮 + LLM 自决** | **业界共识**(Mem0 / Claude Code) —— LLM 语义判断"有没有值得记的事实",异步后台不阻塞 |

业界依据:
- [Claude Code memory docs](https://code.claude.com/docs/en/memory):基于 usefulness heuristic,LLM 自决何时存
- [Letta vs Mem0 vs Zep 对比](https://forum.letta.com/t/agent-memory-letta-vs-mem0-vs-zep-vs-cognee/88):Mem0 每轮提取、Letta agent 自决、Zep 可配置批处理
- [Spheron / AWS AgentCore](https://www.spheron.network/blog/agent-memory-gpu-cloud-mem0-zep-guide):async extraction 是业界标配

### 存储组织

讨论过三种,最终选 **方案 2 分类结构化**:

| 选项 | 否决理由 |
|---|---|
| 1 KV 单表 | 表达力弱,带原因的事实塞 value 别扭 |
| **2 分类结构化** | 对标 Claude Code memory 分类(user/feedback/project/reference),表达力强 + 可按优先级注入 + 前端可观测 |
| 3 文档式 MEMORY.md | LLM 编辑 markdown 不稳定,易删错段落/格式漂移 |

## 4. 架构

新增 Session Facts 层,在 prompt 中作为**最高优先级**注入:

```
┌─ system_prompt ─────────────────────────────────────┐
│ 你是 Porto 知识库问答助手...                          │
└─────────────────────────────────────────────────────┘
┌─ prompt_parts (按注入优先级) ────────────────────────┐
│ 1. 用户问题                  ← 当前消息              │
│ 2. 关键事实 (NEW)            ← 本方案,最高优先       │
│ 3. 会话历史摘要 (强化 a1)    ← 实体感知 prompt       │
│ 4. 最近会话 (sliding window) ← 保持现状              │
│ 5. 记忆检索 (memory.search)  ← 保持现状              │
│ 6. 知识库片段 (KB RAG)       ← 保持现状              │
└─────────────────────────────────────────────────────┘
        ▲
        │ 注入(build_facts_prompt)
┌───────┴─────────────────────────────────────────────┐
│ SessionFactsStore (sqlite session_facts 表)          │
│   每 session 多条 fact,带 category + status          │
└───────▲─────────────────────────────────────────────┘
        │ upsert / retract
┌───────┴─────────────────────────────────────────────┐
│ extract_facts() — 异步后台,每轮 user msg 后触发      │
│   LLM 判断"有没有值得记的事实",无则返回空              │
│   解析失败/LLM 不可用 → fail-open                     │
└─────────────────────────────────────────────────────┘
```

### 组件清单

**新增**:

| 组件 | 文件 | 职责 |
|---|---|---|
| `SessionFactsStore` | `memory/facts.py`(新) | sqlite CRUD:upsert(模糊匹配去重)、retract、list_active |
| `extract_facts()` | `memory/facts.py`(新) | LLM 提取结构化事实,JSON 解析 + 降级 |
| `build_facts_prompt()` | `memory/facts.py`(新) | 按优先级排序 + budget 裁剪,拼 prompt 片段 |
| `trigger_facts_extraction()` | `memory/facts.py`(新) | δ-1 异步触发器:`asyncio.create_task` / `threading.Thread` fire-and-forget |
| `SessionFact` model | `models/payload.py`(改) | dataclass:`id, session_id, category, content, status, source_msg_id, created_at, updated_at` |
| GET `/api/memory/{sid}/facts` | `api/routes/memory.py`(改) | 暴露给前端,可观测 |
| `session_facts` 表 | `memory/store.py:_init_db`(改) | schema migration |

**修改**:

| 组件 | 文件 | 改动 |
|---|---|---|
| `summarize_records` | `memory/compaction.py` | **a1**:prompt 改实体感知 |
| chat 非流式 + 流式 | `api/routes/chat.py:155-168, 283-309` | `memory.add` 之后调 `trigger_facts_extraction`;prompt_parts 插入 facts 片段 |
| `Settings` | `settings.py` | 新增 `facts_*` 配置(详见 §8) |

## 5. 数据模型

`session_facts` 表(在 `memory/store.py:_init_db` 加 migration):

```sql
CREATE TABLE IF NOT EXISTS session_facts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    category TEXT NOT NULL,       -- user_decision | user_preference | project_context | open_question
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',  -- active | retracted
    source_msg_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_session ON session_facts(session_id);
CREATE INDEX IF NOT EXISTS idx_facts_session_cat ON session_facts(session_id, category);
```

**上限保护**:每 session 每 category 最多 `facts_max_per_category`(默认 20)条 active,超过按 `updated_at` 淘汰最旧——防止 facts 膨胀撑爆 prompt。

**`SessionFactsStore` 接口**:

```python
class SessionFactsStore:
    def __init__(self, settings: Settings): ...

    def upsert(
        self, *, session_id: str, category: str, content: str, source_msg_id: str | None
    ) -> str:
        """同 session + category 下,与现有 active facts 做 token Jaccard:
        - ≥ facts_similarity_threshold (默认 0.5) → 更新 content + updated_at
        - < 阈值 → 新增
        超上限 → 删 updated_at 最旧
        返回 fact_id。
        """

    def retract(self, fact_id: str) -> None:
        """标记 status='retracted',不物理删(可审计)。"""

    def list_active(self, session_id: str) -> list[SessionFact]:
        """返回 active facts,按 category 优先级 + updated_at 倒序。"""

    def by_category(self, session_id: str) -> dict[str, list[SessionFact]]:
        """按 category 分组,供 build_facts_prompt 使用。"""
```

**模糊匹配去重**:`_tokenize` 复用 `embeddings.tokens`(CJK 单字+bigram,跟 BM25 同源),计算 Jaccard。不引入新依赖。

## 6. Prompt 设计

### a1 — 实体感知摘要(改 `compaction.py:summarize_records`)

```
你是会话摘要助手。把以下对话历史压缩成简洁的中文摘要。

强制要求:
- 保留所有专有名词、变量名、API 名、产品名原样(不要抽象化成"某功能")
- 保留所有数字、版本号、阈值
- 保留已确认的决策(明确写"用户确认 X")和已否决的选项("用户否决 Y")
- 保留未解决的问题,标注"待澄清:Z"
- 去掉寒暄、试探性提问、重复内容

对话历史:
{transcript}
```

### a2 — Facts 提取(新,异步后台)

```
从以下最新对话中提取值得长期记住的关键事实。

只提取用户明确表达的:
- user_decision: 用户确认或否决的决定
- user_preference: 用户表达的偏好
- project_context: 项目背景、约束、领域信息
- open_question: 待澄清的问题

不提取:agent 提问、寒暄、临时试探。无事实则返回 {"facts": []}。

强制要求:
- content 保留所有专有名词、变量名、数字原样
- 每条事实原子化,不合并多个事实

输出 JSON:
{
  "facts": [
    {"category": "user_decision", "content": "登录采用 OAuth", "action": "add"},
    {"category": "user_decision", "content": "不用 SAML", "action": "add"}
  ]
}

action 语义:
- add / amend: 实现上等价,都走 SessionFactsStore.upsert(模糊匹配 ≥ 阈值则更新,否则新增)。两个 action 名只是给 LLM 的语义提示,让它知道"可以新增也可以修正已有事实",避免它把改主意当成新增一条矛盾的 fact
- retract: 撤销事实(用户改主意,如"不用 OAuth 了"),走 SessionFactsStore.retract,与 add/amend 是不同代码路径

最新用户消息:
{new message}

最近上下文:
{recent N turns}
```

**JSON 解析**:复用 `llm/parsing.py` 现有的 JSON fence/bare 提取逻辑;解析失败 → log warning,跳过本轮(fail-open)。

### 注入(`build_facts_prompt`)

```
关键事实(用户已确认,优先参考):
[决策]
- 登录采用 OAuth
- 不用 SAML
[偏好]
- 后端用 Go
[背景]
- 这是给金融客户的项目
[待澄清]
- 前端框架未定
```

- 按 category 分组,顺序:`user_decision` > `user_preference` > `project_context` > `open_question`
- 空组不渲染
- 无任何 active fact → 返回空串,prompt_parts 不插入该段
- 接进现有 `_trim_to_budget`(从后向前裁,facts 在前=最后被裁)

## 7. 数据流

```
user message 到达 chat() / chat_stream()
  │
  ├─ [同步,阻塞] 现有链路:
  │    memory.search(req.message, session_id)
  │    get_compacted_history(session_id) → summary, recent
  │    KB store.search(req.message)
  │    memory.add(user message)
  │    prompt_parts 拼装:
  │      [用户问题, 关键事实(NEW), 摘要(a1 强化), 最近会话, 记忆检索, KB 片段]
  │    → _trim_to_budget
  │    → llm.complete / stream → 返回用户
  │
  └─ [异步,fire-and-forget] trigger_facts_extraction(session_id, new_msg, recent):
       流式路径(async): asyncio.create_task(extract_facts_async(...))
         注意:extract_facts_async 内部调 LLM 必须用 await llm.acomplete 或
         asyncio.to_thread(llm.complete, ...),否则同步 httpx 会阻塞事件循环、
         卡住同一 event loop 上的其他 SSE 流
       非流式路径(sync): threading.Thread(target=extract_facts, daemon=True).start()
       
       extract_facts():
         LLM 不可用 → log info, return
         LLM 输出 JSON → 解析
         解析失败 → log warning, return
         对每条 fact:
           action=add/amend → SessionFactsStore.upsert(...)
           action=retract    → 模糊匹配找到对应 active fact → retract(...)
         log info extracted=N

注:当前轮 chat 用不到本轮新提取的 facts(异步未完成);下一轮 chat 时 facts 已就位。
    痛点是"多轮场景下 agent 接得上",滞后一轮完全可接受。
```

**chat.py 改动点**(两条路径同步改):

```python
# 非流式 chat()
memories = memory.search(req.message, session_id=req.session_id, top_k=5)
summary, recent = get_compacted_history(req.session_id, memory, llm)
memory.add(session_id=req.session_id, role="user", content=req.message)

facts_store = SessionFactsStore(runtime_settings)
facts_block = build_facts_prompt(facts_store.list_active(req.session_id), runtime_settings)
trigger_facts_extraction(req.session_id, req.message, recent, llm, runtime_settings)  # 异步

prompt_parts = [f"用户问题:\n{req.message}"]
if facts_block:
    prompt_parts.append(facts_block)              # NEW: 最高优先级
if summary:
    prompt_parts.append(f"会话历史摘要:\n{summary}")
prompt_parts.append("最近会话:\n" + "\n".join(f"{m.role}: {m.content}" for m in recent))
prompt_parts.append(f"记忆检索:\n{format_sources(memories)}")
prompt_parts.append(f"知识库片段:\n{format_sources(sources)}")
```

## 8. 配置(`settings.py` 新增)

```python
# Session facts (a2)
facts_enabled: bool = True
facts_max_per_category: int = Field(default=20, ge=1, le=100)
facts_similarity_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
facts_recent_context_turns: int = Field(default=6, ge=1, le=20)  # 提取时喂给 LLM 的最近轮数
# 提取用的 LLM:缺省回退到 agent_*
facts_provider: Literal["openai", "anthropic"] | None = None
facts_model: str | None = None
```

**LLM 选型**:缺省复用 `LLMClient`(agent 同模型);预留 `facts_provider`/`facts_model` 字段方便后续切轻量模型(对标 KB 已有的 `rerank_provider`/`rerank_model` 模式)。

## 9. 错误处理 / 降级

| 场景 | 处理 |
|---|---|
| `facts_enabled=False` | 完全跳过提取与注入,行为 = 现状 |
| LLM 未配置 key | 跳过提取,log info,fail-open |
| LLM 调用超时/网络错 | 异步任务内 try/except,log warning,不重试(下一轮再触发) |
| JSON 解析失败 | log warning,跳过本轮 |
| 提取返回空 `{"facts": []}` | 正常,不写库 |
| 超 category 上限 | 淘汰 `updated_at` 最旧 |
| 异步任务进程重启丢失 | 可接受,下一轮用户消息重新触发 |
| 注入超 budget | `_trim_to_budget` 兜底,facts 在前=最后被裁 |
| 模糊匹配无命中 retract | log info,跳过(找不到对应 fact 就不撤) |

**铁律**:facts 任何环节失败都不阻塞主 chat 链路——它是"锦上添花",不是"命脉"。

## 10. 测试策略

**单测**(`tests/memory/test_facts.py`):
- `SessionFactsStore.upsert`:新增 / 模糊匹配更新(Jaccard ≥ 阈值)/ 低于阈值新增 / 上限淘汰最旧
- `SessionFactsStore.retract`:标记 retracted / 已 retracted 再 retract 幂等
- `SessionFactsStore.list_active`:过滤 retracted / 按 category 优先级排序
- `extract_facts`:JSON 解析成功 / 空结果 / 解析失败降级 / LLM 不可用降级
- `build_facts_prompt`:分类分组 / 优先级排序 / 空组不渲染 / 全空返回空串 / budget 裁剪
- `summarize_records`(a1):新 prompt 实体保留(fixture 对比关键词是否保留)
- `trigger_facts_extraction`:fire-and-forget 不阻塞(用 `asyncio` mock 验证不 await)

**集成测**(`tests/api/test_chat_facts.py`):
- 非流式 chat:facts 被注入 prompt_parts(检查拼出的字符串)
- 流式 chat_stream:facts 注入 + 异步任务被触发(不阻塞 SSE)
- 多轮场景:facts 跨轮累积(第一轮提取 → 第二轮注入)

**回归**(`tests/memory/test_compaction.py`):
- a1 新 prompt 不破坏现有 compaction 行为(summary 仍生成、缓存仍命中)

**fail-open 回归**:
- facts 为空时 chat 行为 = 现状
- facts 提取全程失败时 chat 不报错

## 11. 与 2026 RAG 指南的最终对照

| 指南建议 | 本次采纳情况 |
|---|---|
| Tiered Memory(short/mid/long) | ✅ 补齐 long-term facts 层(short/mid 已有) |
| Async extraction(每轮后台提取) | ✅ δ-1 异步触发 |
| LLM as memory decision maker | ✅ LLM 自决记什么(不靠关键词) |
| 实体感知摘要 | ✅ a1 强化 prompt |
| 结构化分类记忆 | ✅ 对标 Claude Code memory 分类 |
| Hybrid + RRF + Reranker(Agent Memory 层) | ❌ 本次不做(YAGNI,先看 a1+a2 效果) |
| BGE-M3 / ColBERTv2(KB 层) | ❌ KB 不动 |

## 12. 后续可能(本 spec 不做)

- **(b) 查询改写**:若 a1+a2 仍不够,再上 LLM 把省略句改写成完整查询
- **(c) Agent Memory 也上 hybrid+rerank**:若 facts 层不够,再把 KB 那套搬过来
- **跨 session 长期记忆**:若用户需求扩展,把 session_facts 提升为 user_facts
- **前端 facts 管理 UI**:可观测 API 落地后,前端做编辑/删除/确认交互
