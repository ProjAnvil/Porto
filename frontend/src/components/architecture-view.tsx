"use client";

import { MermaidDiagram } from "./mermaid-diagram";

// ── 图 1：Intent router（问话方式）──
// 来源：docs/backend-agent-guide.md §6
const INTENT_ROUTER = `flowchart LR
    M[用户消息] --> Q{LLM 可用?}
    Q -->|是| LS[LLM complete_structured<br/>输出 intent: direct/rag]
    LS --> D[用 LLM 判断结果]
    Q -->|否| RS[规则: 正则 + 关键词]
    RS --> D`;

// ── 图 2：Streaming（问话方式）──
// 来源：§9
const STREAMING = `sequenceDiagram
    participant FE as 前端
    participant API as chat_stream
    participant LLM as 大模型
    FE->>API: POST /api/chat/stream
    API->>API: prep(意图/检索/记忆/拼prompt)
    loop LLM token 流
        LLM-->>API: delta token
        API-->>FE: SSE text-delta
    end
    API->>FE: SSE source-document / data-porto / finish`;

// ── 图 3：Memory 三层存储 + compaction（Context 组装）──
// 来源：§7
const MEMORY = `flowchart TB
    subgraph STOR["存储"]
        A[(SQLite<br/>每条消息原文)]
        B[(ChromaDB<br/>消息向量, 语义检索)]
        C[(SQLite<br/>session 摘要缓存)]
    end
    Msg[新消息] --> A
    Msg --> B
    Query[检索] --> B
    Query --> Hist[历史]
    Hist --> Check{条数 > 阈值?}
    Check -->|否| Out1[全部原文]
    Check -->|是| Split[旧消息 + 近期N条]
    Split --> Cache{摘要已缓存?<br/>by last_message_id}
    Cache -->|是| Reuse[复用摘要]
    Cache -->|否| Sum[LLM 摘要旧消息]
    Sum --> C
    Reuse --> Out2[摘要 + 近期原文]
    Sum --> Out2`;

// ── 图 4：Prompt 拼装顺序 + 预算截断（Context 组装）──
// 来源：§8（新建图，guide 中为纯文字描述）
const PROMPT_ASSEMBLY = `flowchart LR
    Q[用户问题] --> S[会话历史摘要<br/>来自 compaction]
    S --> R[近期会话原文<br/>recent N 条]
    R --> M[记忆检索<br/>向量检索相关历史]
    M --> KB[知识库片段<br/>RAG 检索]
    KB --> MERGE[合并 prompt]
    MERGE --> BUDGET{总字符超<br/>context_char_budget?}
    BUDGET -->|是| TRIM[_trim_to_budget<br/>从后向前截断检索片段]
    BUDGET -->|否| OUT[发送给 LLM]
    TRIM --> OUT`;

// ── 图 5：LangGraph 状态机（Workflow loop）──
// 来源：§4（Send fan-out + spec 子图，无 evaluate）
const LANGGRAPH = `stateDiagram-v2
    [*] --> retrieve: prd_file_id (pointer)
    retrieve --> understand: + sources
    understand --> identify: + understanding
    identify --> generate: Send fan-out (并发=spec_refine_concurrency)
    generate --> [*]: 各 spec 独立交付（无 evaluate）`;

// ── 图 6：Tool calling loop（Workflow loop）──
// 来源：§3
const TOOL_LOOP = `sequenceDiagram
    participant Node as Agent 节点
    participant TL as tool-loop LLMClient
    participant LLM as 大模型
    participant Exec as 本地 tool 执行器
    Node->>TL: system + user + tools(schema)
    loop 直到 LLM 不再要工具 或 达到 max_turns
        TL->>LLM: 当前对话 + 可用工具
        LLM-->>TL: tool_call 或 最终文本
        alt 返回 tool_call
            TL->>Exec: 调对应 handler
            Exec-->>TL: 执行结果
            TL->>TL: 结果作为 tool_result 回填
        else 返回文本
            TL-->>Node: 最终答案 退出循环
        end
    end`;

// ── 图 7：FileService（Memory Pointer 模式）──
// 来源：design §3-§4，统一文件访问层
const FILE_SERVICE = `flowchart TB
    UP[用户上传文件] --> ST[FileService.store<br/>owner_id]
    ST --> DISK[(落盘<br/>data_dir/files/file_id/<br/>原文 + pages_json)]
    ST --> META[(SQLite 元数据<br/>page_count / size / mime)]
    DISK --> SPLIT{场景}
    SPLIT -->|workflow| WF["state.prd_file_id<br/>(pointer, 不存全文)"]
    SPLIT -->|chatbot| CB[文件关联 session<br/>owner_id = session_id]
    WF --> NODES[retrieve / understand / identify<br/>节点按需读取]
    CB --> AGENT[Agent SDK Claude<br/>自主调 read_file tool]
    NODES --> READ[FileService<br/>read_pages / search / get_info]
    AGENT --> READ
    READ -.读取.-> DISK`;

// ── 图 8：检索优化管线（query transform → hybrid → rerank）──
// 来源：backend/src/porto_chatbot/retrieval.py、query_transform.py、settings.py
const RETRIEVAL_OPTIMIZATION = `flowchart TB
    Q[用户 query] --> QT{query transform<br/>策略?}
    QT -->|none| RAW[原 query 直接检索]
    QT -->|hyde| HYDE[HyDE<br/>LLM 生成假设性答案<br/>用答案去检索]
    QT -->|multi_query| MQ[Multi-Query<br/>改写 N 个变体<br/>各自检索后 RRF 融合]
    QT -->|decomposition| DEC[Decomposition<br/>拆成子问题<br/>分别检索后合并去重]
    QT -->|step_back| SB[Step-Back<br/>抽象成背景问题再检索]
    HYDE --> FUSE
    MQ --> FUSE
    DEC --> FUSE
    SB --> FUSE
    RAW --> FUSE
    subgraph FUSE["混合检索 hybrid fusion"]
        V[向量检索<br/>Chroma 语义相似度]
        K[BM25 关键词检索<br/>CJK 预分词]
        V --> RRF[RRF 融合<br/>hybrid_vector_weight 调权]
        K --> RRF
    end
    FUSE --> RR{rerank_enabled?}
    RR -->|是| LR[LLM Rerank<br/>候选精排取 rerank_top_n]
    RR -->|否| OUT[top_k 结果]
    LR --> OUT
    QT -. LLM 失败 .-> DEG[fail-open<br/>回退原 query 检索<br/>链路永不中断]
    LR -. 异常 .-> DEG`;

function DiagramSection({
  title,
  description,
  chart,
}: {
  title: string;
  description: string;
  chart: string;
}) {
  return (
    <div className="mb-8">
      <h3 className="mb-1 text-sm font-semibold text-zinc-900">{title}</h3>
      <p className="mb-3 text-xs leading-5 text-zinc-500">{description}</p>
      <div className="rounded-xl border border-zinc-200 bg-white p-4">
        <MermaidDiagram chart={chart} />
      </div>
    </div>
  );
}

export function ArchitectureView() {
  return (
    <div className="max-w-3xl">
      {/* 系统定位 */}
      <div className="mb-6 rounded-lg border border-sky-200 bg-sky-50 p-4">
        <p className="text-sm font-medium text-sky-900">系统定位</p>
        <p className="mt-1 text-xs leading-5 text-sky-700">
          Porto 是固定 workflow 骨架 + 节点内 agentic 的混合架构。主路径预先编排好
          （retrieve → understand → identify → generate），identify 末以 Send fan-out
          并发启动 spec 子图；每个节点内部 LLM 用 tool-calling loop 自主取数，子图内
          用 evaluator-optimizer loop 自我精修并独立交付。
        </p>
      </div>

      {/* 问话方式 */}
      <h2 className="mb-4 text-base font-semibold">问话方式</h2>
      <DiagramSection
        title="Intent Router：direct vs RAG"
        description="聊天入口先用 LLM（或规则降级）判断这句话走直接回答还是 RAG 检索。LLM 不可用时退回正则 + 关键词匹配。"
        chart={INTENT_ROUTER}
      />
      <DiagramSection
        title="Streaming：聊天 token 流"
        description="prep 阶段（意图/检索/记忆/拼 prompt）非流式一次性完成，答案生成阶段原生 token 流式返回。LLM 不可用时退回 complete 假流式。"
        chart={STREAMING}
      />

      {/* Context 组装 */}
      <h2 className="mb-4 text-base font-semibold">Context 组装</h2>
      <DiagramSection
        title="Memory 三层存储 + Compaction"
        description="原文（SQLite）、向量（ChromaDB）、摘要缓存（SQLite）。会话超阈值时旧消息 LLM 摘要压缩，拼 prompt 用「摘要 + 近期原文」。"
        chart={MEMORY}
      />
      <DiagramSection
        title="Prompt 拼装 + 预算截断"
        description="按固定顺序叠加：用户问题 → 会话摘要 → 近期原文 → 记忆检索 → RAG 片段。总字符超 context_char_budget 时从后向前截断检索片段。"
        chart={PROMPT_ASSEMBLY}
      />

      {/* 检索优化 */}
      <h2 className="mb-4 text-base font-semibold">检索优化</h2>
      <DiagramSection
        title="检索优化管线：Query Transform → Hybrid Fusion → Rerank"
        description="三级优化策略：① Query Transform 用 LLM 改写 query（HyDE 假设性答案 / Multi-Query 多变体 + RRF 融合 / Decomposition 子问题拆解合并 / Step-Back 背景抽象），chat 与 workflow 分别由 chat_query_transform_strategy、workflow_query_transform_strategy 配置，默认 none。② Hybrid Fusion 向量（Chroma 语义）+ BM25（CJK 预分词关键词）双路候选，RRF 融合并用 hybrid_vector_weight 调权。③ Rerank 用 LLMRerank 对候选精排取 rerank_top_n（rerank_enabled 控制，默认关）。所有 LLM 步骤均 fail-open：失败即回退原 query 检索，链路永不中断。"
        chart={RETRIEVAL_OPTIMIZATION}
      />

      {/* 文件服务（Memory Pointer）*/}
      <h2 className="mb-4 text-base font-semibold">文件服务（Memory Pointer）</h2>
      <DiagramSection
        title="FileService：统一文件访问层"
        description="store 落盘 + 预提取 pages_json，元数据入 SQLite。state 只存 prd_file_id（pointer，不存全文），workflow 节点与 chatbot agent 共用同一 FileService.read_pages/search/get_info 按需读取。"
        chart={FILE_SERVICE}
      />

      {/* Workflow loop */}
      <h2 className="mb-4 text-base font-semibold">Workflow Loop</h2>
      <DiagramSection
        title="LangGraph 状态机"
        description="主路径 retrieve→understand→identify→generate，state 只带 prd_file_id pointer 不存全文。identify 末以 Send fan-out（并发=spec_refine_concurrency，Semaphore 限流）启动 spec 子图，各 spec 在子图内部 critique→refine 自我精修后独立交付。"
        chart={LANGGRAPH}
      />
      <DiagramSection
        title="Tool Calling Loop（节点内 agentic）"
        description="每个节点内部 LLM 反复选工具 → 执行 → 回填 → 再思考，直到不再要工具或达到 max_turns。这是 function calling 的工程化实现。"
        chart={TOOL_LOOP}
      />
    </div>
  );
}
