# Vectra Knowledge Base Compatibility — Research & Design

**Date**: 2026-10-17（v2，按能力面复盘重写）
**Status**: Implemented
**Scope**: Porto 兼容 SCV v0.10+ 产出的 Vectra 知识库（`structure.vg`）

**已决策**（2026-10-17）
1. 内置纯 Python `.vg` 客户端作为**能力底座**（Tier 0），保证零 Go 工具链也能用。
2. **叠加 MCP 通道**（Tier 1）取 Cypher 能力——此前判定「Cypher 是过度设计」是错的，已撤回。
3. 加载面 = 全部（component + structure 双面）。

---

## 1. 结论

Vectra 是通用图引擎，不是 SCV 专用格式。Porto 应当**按能力分层取用**：

- **Tier 0（常开，零依赖）**：纯 Python 解析 `.vg`，提供邻居、BFS 影响面、SCC/环、属性过滤。
- **Tier 1（可选，有 Go 二进制时）**：MCP 调 `scv-graph-mcp`，取 Cypher（聚合、变长边、WHERE、ORDER BY）。

两条通道能力递增、接口统一，Tier 1 不可用时自动退回 Tier 0，Tier 0 不可用时退回空图。
这复刻了 SCV 自己的分层哲学（structure.json → structure.vg → MCP hint）。

---

## 2. Vectra 能力盘点：Porto 用什么、不用什么

### 2.1 采用

| 能力 | Porto 用途 | 层级 |
|---|---|---|
| Cypher 聚合 `count/sum/avg/min/max/collect` + 隐式分组 | 子系统体量画像、stereotype 普查、包级 Bean 计数 → 喂 `identify` | Tier 1 |
| Cypher 变长边 `*1..2` 传递影响面 | 「改这个模块波及哪些」→ 喂 `generate` 与 rubric | Tier 1 |
| Cypher `WHERE`（`STARTS WITH` / `CONTAINS` / `IN` / 比较） | 按包前缀、stereotype、规模筛选 | Tier 1 |
| Cypher `ORDER BY` + 数值属性 `loc` | 复杂度热点排序（最大的类 / 最长的方法） | Tier 1 |
| BFS（多源、带深度、按 label 过滤） | 影响面主干，最高频操作 | Tier 0 |
| SCC / `Cycles()`（边带 props） | 循环依赖及其严重度（fatal / tolerated） | Tier 0 |
| `stats.sccs` / `cyclicNodes` | 架构健康度信号 → 喂 `identify` | Tier 0 |
| 属性等值查找（stereotype / beanName / type） | 锚点定位、stereotype 普查 | Tier 0 |
| `shortest_path` | 分层违规检测（Controller 直连 Repository？） | Tier 1 |
| `reachable` | 完整影响闭包（**必须加 limit**，上游无内置上限） | Tier 1 |

### 2.2 不采用（附理由，非「过度设计」式驳回）

| 能力 | 不采用的理由 |
|---|---|
| OCC 事务 `/v1/txn` | Porto 是只读消费者，无并发写，用不上 SI/SER 隔离 |
| WAL / 水位快照 / manifest | SCV 用 `SaveFile` 产出密封快照，无 WAL；Porto 只读 |
| raft 集群 / `/v1/join` | 单节点即可；引入集群是运维负担，不是 Porto 的职责 |
| HTTP server（13 端点） | 需常驻 raft 进程 + data 目录，且 API 零认证零 TLS；MCP 通道已覆盖查询需求 |
| `LookupByNumberRange` | 数值属性只有 `loc`，Cypher 的 `WHERE`/`ORDER BY` 已覆盖，不必单独接 |
| `inject_batch` / `seal`（写） | 见 §6，评估为**有价值的候选但非本期** |
| `FoldLayers` / merge 内部 | 引擎内部机制，与 Porto 无关 |

### 2.3 明确的能力边界（写进实现约束）

- Cypher **只读**：无 `CREATE`/`MERGE`/`SET`/`DELETE`/`WITH`/`UNION`/`SKIP`。
- **无 `GROUP BY`**：分组键 = 所有非聚合的 RETURN 项。
- 变长边**不绑定边变量**（引用即解析错误）。
- 变长边是**最短距离语义**，中间节点不被目标模式过滤。
- 中间行上限 1,000,000；MCP/HTTP 层默认截断 1000 行，`total` 报真实数。
- `lookup_property` 对**未索引的属性是硬失败**，不是返回空。SCV 已声明
  `stereotype`/`beanName`/`type`/`conditions` 四个索引键。
- `direction` 只接受 `"out"`/`"in"`/`""`，其他值报错（不静默回退）。
- Cypher **没有向量/相似度能力**——语义检索仍由 Porto 的 embedding 承担。

---

## 3. SCV 图契约（权威版）

来源：`scv-graph-mcp` 的 `cypher` tool 描述（`SCV/perception/graphmcp/main.go:235-247`），
比 README 更完整，**以此为准**。

### 节点

| Label | Key 格式 | props |
|---|---|---|
| `Bean` | `bean:<name>` | `beanName`、`type`、`stereotype`（RestController/Service/Repository/Configuration/…）、`conditions`、`source` |
| `Type` | `type:<fqn>` | `kind`（class/interface/record/enum/annotation）、`modifiers`、**`loc`** |
| `Method` | `fn:<fqn>#<name>(<params>)` | `name`、`visibility`、`static`、`params`、`returns`、`owner`（Type key）、**`loc`** |
| `File` | `file:<repo-relative path>` | `path`、`language`、**`loc`** |

### 边

| Label | 端点 | props |
|---|---|---|
| `INJECTS` | Bean → Bean | `via`（constructor/lombok-constructor/beanMethod/field/setter）、`qualifier`、`wraps`、`ambiguous` |
| `CALLS` | Method → Method | `line`、`kind`（direct / virtual-dispatch） |
| `IMPLEMENTS` / `EXTENDS` | Type → Type | — |
| `CONTAINS` | File → Type | — |
| `REFERENCES` | Method → Type | `line` |

**修正此前判断**：`loc` 是数值属性，所以「按规模排序/筛选」这类查询**可用**
（`MATCH (t:Type) RETURN t.key, t.loc ORDER BY t.loc DESC LIMIT 10`）。
此前认为「SCV 无数值属性、LookupByNumberRange 无用」是错的。

### 产物位置

- `~/.scv/analysis/<repo>/structure.vg` —— Vectra v3 密封快照
- `~/.scv/analysis/<repo>/structure.json` —— 同源 JSON
- 由 `SCV/perception/loader/main.go:134` 的 `gr.SaveFile()` 写出，**无 WAL、无 manifest**

### 快照格式 v3（小端，已字节级实测）

```
"VCTR" | u16 version(3) | u32 node_count | u32 edge_count
  | node{ key, label, props }*      # string = u32 len + utf8; props = u32 len + JSON (0 = nil)
  | edge{ srcKey, dstKey, label, props }*
  | u32 indexKey_count | key string*
  | u32 CRC32(IEEE) 覆盖此前全部字节
```

---

## 4. 架构

### 4.1 分层

```
backend/src/porto_chatbot/graph/
├── base.py           # StructureGraph Protocol + GraphNode/GraphEdge/GraphStats + 异常
├── snapshot.py       # Tier 0：v3 解码器（纯标准库）
├── local_graph.py    # Tier 0：内存邻接表 + neighbors/bfs/cycles/lookup/stats
├── mcp_client.py     # Tier 1：MCP over stdio（JSON-RPC，进程生命周期管理）
├── discovery.py      # 发现 structure.vg + 选择 Tier 1 或 Tier 0 + mtime 缓存
├── context.py        # 图上下文渲染（锚点定位 → 摘要文本）
└── __init__.py
```

**不进 `vector_stores/`**。Vectra 不是向量后端，不满足 `VectorRepository` 协议
（无 embedding、无 dimension、无 top-k 相似度）。它是平级的第二检索通道。

### 4.2 能力分层

| | Tier 0 `local_graph` | Tier 1 `mcp_client` |
|---|---|---|
| 依赖 | 零（标准库） | Go 二进制在 PATH 或已知路径 |
| 图存放 | Python 进程内存 | Go 进程内存 |
| 能力 | neighbors / bfs / cycles / lookup / stats | 上述全部 **+ Cypher + shortest_path + reachable** |
| 规模 | 受 Python 内存约束（见 §4.4） | 由 Go 承担，Porto 侧只拿结果 |
| 单仓/多仓 | 按 repo 懒加载 | `scv-graph-mcp` 单进程服务全部 repo |

**二进制发现顺序**（对齐 SCV 的 `perceive.py:453-463` 做法）：
1. `scv-graph-mcp`（首选：单进程、多仓、有 `repos`/`cypher(repo,…)`/`get_node(repo,…)`/`refresh`）
2. `vectra-mcp -snapshot <vg>`（回退：单仓，11 tool）
3. 都没有 → Tier 0

`scv-graph-mcp` 支持 `-analysis-dir <dir>`，Porto 用它指向 `kb_dirs[0]`。

### 4.3 Tier 1 进程模型

- 懒启动：首次图查询时才 spawn。
- 长驻复用：`subprocess.Popen` 单例，非每请求启停。
- 生命周期：`initialize` → `notifications/initialized`；崩溃自动重启一次；
  FastAPI lifespan 关闭时 terminate。
- 超时：`tools/call` 每次带超时，超时即标记 Tier 1 不可用并退回 Tier 0。
- **只读**：只用 `cypher` / `get_node` / `repos` / `neighbors` / `bfs` / `cycles` / `stats`，
  绝不调用 `inject_batch` / `seal`（SCV 拥有 `structure.vg`，Porto 不得修改）。

### 4.4 Tier 0 规模控制（双面加载，强制项）

| 规模 | 文件 | 解码 | 峰值 RSS |
|---|---|---|---|
| 66k 节点 | 6.7 MB | 0.09 s（lazy 0.05 s） | ~91 MB |
| 220k 节点 | 22.7 MB | 0.32 s（lazy 0.19 s） | ~225 MB |

四道闸：
1. **props 惰性解码**：存原始字节，访问时才 `json.loads`。实测省 40% 解码耗时。
2. **mtime 缓存**：`<repo>/structure.vg` 的 mtime 未变则直接复用。
3. **节点上限** `graph_max_nodes`（默认 300_000）：超限返回空图 + warning，不抛。
4. **按 repo 懒加载**：命中该 repo 才解析。

Tier 1 可用时优先走 Tier 1——规模由 Go 承担，Python 侧无内存压力。

### 4.5 遍历约束

**必须按边 Label 过滤**，默认 `["INJECTS"]`。双面加载后不过滤 label 的 BFS 会从
`Bean` 经 `CONTAINS` 扩散到全仓 `Method` 节点，噪声大于信号。

| 查询意图 | Label 集合 |
|---|---|
| 组件影响面 | `INJECTS` |
| 调用链 | `CALLS` |
| 类型继承面 | `IMPLEMENTS`、`EXTENDS` |
| 文件归属 | `CONTAINS` |

---

## 5. 集成点（按收益排序）

### P0 — `identify` 节点（收益最大，当前最弱的一步）

`agent/nodes/identify.py:26-38` 现状：纯 LLM，输入**只有** `understanding` + PRD 前 2000 字；
`state["sources"]` 完全没被用到；且该步不传 `tools`（`backends.py:106-111`
`structured_schema` 与 `tools` 互斥）。LLM 路径下**零代码信号**。

改动：把图摘要拼进 `identify` 的 user prompt。摘要内容：
- 仓库列表与规模（`repos` / `stats`）
- **SCC 与环**：`cycles()` → 强耦合簇即天然子系统边界
- **stereotype 普查**：`MATCH (n:Bean) RETURN n.stereotype, count(*)`
- **复杂度热点**：`MATCH (t:Type) RETURN t.key, t.loc ORDER BY t.loc DESC LIMIT 20`

### P0 — `generate` 的 system prompt

`specs/steps.py:45-51` 现状：system 只有 `sub.name` / `responsibility` / `capabilities` + 章节清单。
改动：追加该子系统对应图节点的影响面摘要（BFS / Cypher 变长边）。

### P1 — `retrieve` 节点

`agent/nodes/retrieve.py:20-22`：图上下文并入 `state["sources"]`（用 `SourceChunk`
承载，`path` 放 FQN），下游工具与 prompt 自动受益。

### P1 — 新工具 `search_code_graph`

与 `search_knowledgebase`（`tools/handlers.py:61`）同构，让 LLM 在 understand/generate
里自主查图。需：`handlers.py` + `registry.py:19` + `agent_sdk/tools.py:55`（双后端镜像）
+ `AgentToolContext` 加图句柄（`tools/context.py:36`）。

### P2 — rubric 增维

`specs/rubric.py:9-16` 现状 6 维，**全部是文本语义检查，无任何模块级覆盖检查**。
新增第 7 维「模块覆盖」：spec 是否覆盖了图影响面里的模块。
注意：满分 12 → 14，需同步 `spec_refine_pass_score`（`settings.py:88`，默认 10）。

---

## 6. 写回能力评估（诚实结论）

Vectra 支持 `inject_batch` / `seal`，理论上 Porto 可以把
`spec → 子系统 → 模块` 的追溯关系写进图，形成需求追溯图。

**本期不做**，三条理由：
1. `structure.vg` 由 SCV 拥有并重生成，Porto 写入会在下次 `/scv run` 时被抹掉。
2. 若 Porto 自建图文件（如 `~/.porto/spec_graph.vg`），需引入写路径、
   单写者纪律、merge 语义——复杂度跳一个量级。
3. `evaluate` 节点已移除（`agent/graph.py:24` `STEPS` 只有 4 步），
   追溯图的消费方目前不存在。

**保留为候选**：若后续重建 `evaluate` 或做「模块 → 需返工的 spec」反向查询，
再评估自建图文件。

---

## 7. 风险

| 风险 | 等级 | 缓解 |
|---|---|---|
| 快照格式演进（v0.4/0.5/0.7 三次不兼容） | 中 | 只支持 v3；version≠3 降级为空图 + 记 step，不崩；钉 Vectra v0.7.1 |
| Tier 0 大图内存与解析耗时 | 中 | §4.4 四道闸；Tier 1 可用时优先走 Tier 1 |
| Tier 1 进程管理（Porto 生产代码首个 subprocess） | 中 | 懒启动 + 单例 + 超时 + 崩溃重启 + lifespan 关闭；不可用时自动退回 Tier 0 |
| SCV 未跑 perception（无 `.vg`） | 低 | 天然降级，Porto 退回现有 Markdown 检索 |
| Cypher 表达力边界（无 GROUP BY / 无写） | 低 | 实现约束里写死；generate 的查询由 Porto 预置，不由 LLM 自由生成 |

---

## 8. 开放问题

1. **`identify` 要不要改成带 tools？** 当前 `execute_node` 的 `tools` 与
   `structured_schema` 互斥（`backends.py:97-113`）。改它影响面较大；
   本期先用「拼进 prompt」的方式，不改调度层。
2. **rubric 加维是否在本次做？** 会改满分（12→14）与 `spec_refine_pass_score`。
   建议拆为独立任务、最后做，避免影响 refine loop 基线。

---

## 附录 A：验证方法

1. **格式验证**：Go 生成 `structure.vg`（复刻 SCV loader 的
   `IndexProperty → AddNode → UpsertEdge → Seal → SaveFile`），
   Python 解码器逐字段比对，CRC32-IEEE 校验通过，
   并与 Go `vectra.LoadFile()` + `Cypher()` 交叉验证一致。
2. **MCP 验证**：raw JSON-RPC over stdio 完成
   `initialize → notifications/initialized → tools/list → tools/call(cypher)`，
   确认 11 tool 且 `structuredContent` 直出。
3. **规模验证**：66k 与 220k 节点图，测 Python 解码耗时与内存。
