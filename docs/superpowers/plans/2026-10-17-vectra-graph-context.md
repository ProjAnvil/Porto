# Implementation Plan — Vectra Knowledge Base Compatibility

**Date**: 2026-10-17（v2）
**Design**: [`../specs/2026-10-17-vectra-graph-context-design.md`](../specs/2026-10-17-vectra-graph-context-design.md)
**Status**: Implemented（任务 1–20 全部完成，见文末「实施记录」）

## Goal

让 Porto 读取 SCV v0.10+ 产出的 `~/.scv/analysis/<repo>/structure.vg`，把代码结构图
作为第二检索通道接入，并注入 `identify` / `generate` 两个最弱的节点。

**能力分层**：Tier 0 纯 Python 解码器（常开、零依赖）+ Tier 1 MCP 通道（可选、取 Cypher）。
Tier 1 不可用自动退回 Tier 0，Tier 0 不可用退回空图。

## 硬约束

- **不新增运行时依赖**。Tier 0 只用 `struct`/`json`/`zlib`；Tier 1 只用 `subprocess`+`json`。
- **不碰 `vector_stores/`**。Vectra 不满足 `VectorRepository` 协议。
- **fail-open 是硬要求**。图不可用一律返回空图，绝不把异常抛到调用方。
- **只读**。绝不调用 `inject_batch` / `seal`——SCV 拥有 `structure.vg`。
- **TDD**。每个任务先写会失败的测试。
- 风格：ruff `line-length=100`、Python 3.12、对外契约 docstring 英文、
  解释「为什么」的注释中文、公开 API 用 numpy 风格 docstring。

## 环境

```bash
cd backend && uv sync && uv run pytest
```

---

# Phase 1 — Tier 0 底座

## 任务 1：包骨架 + 数据类型

- 文件：`backend/src/porto_chatbot/graph/__init__.py`、`graph/base.py`（新建）
- 要做的：复制 `vector_stores/base.py:15-49` 的 dataclass 风格，定义：

```python
"""Backend-neutral types and protocol for code-structure graphs."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence, runtime_checkable


@dataclass(frozen=True)
class GraphNode:
    """A node in a structure graph.

    Attributes
    ----------
    key:
        Stable string key, e.g. ``bean:orderService`` / ``type:com.acme.T``.
    label:
        Node label: ``Bean`` / ``Type`` / ``Method`` / ``File``.
    props:
        Node properties decoded from the snapshot.
    """

    key: str
    label: str
    props: dict = field(default_factory=dict)


@dataclass(frozen=True)
class GraphEdge:
    """A directed, labelled edge between two node keys."""

    src: str
    dst: str
    label: str
    props: dict = field(default_factory=dict)


@dataclass(frozen=True)
class GraphStats:
    """Size and health counters for one loaded graph."""

    nodes: int = 0
    edges: int = 0
    index_keys: tuple[str, ...] = ()
    sccs: int = 0
    cyclic_nodes: int = 0


class SnapshotError(Exception):
    """Malformed or unsupported snapshot bytes.

    Callers must catch this and degrade to an empty graph.
    """


@runtime_checkable
class StructureGraph(Protocol):
    """Every structure-graph backend implements this protocol."""

    def node(self, key: str) -> GraphNode | None: ...
    def neighbors(self, key: str, direction: str = "out",
                  labels: Sequence[str] | None = None) -> list[GraphNode]: ...
    def bfs(self, keys: Sequence[str], max_depth: int = 2,
            labels: Sequence[str] | None = None,
            limit: int = 50) -> list[tuple[GraphNode, int]]: ...
    def cycles(self) -> list[tuple[tuple[str, ...], str]]: ...
    def lookup(self, prop_key: str, prop_value: object,
               limit: int = 50) -> list[GraphNode]: ...
    def stats(self) -> GraphStats: ...
    def cypher(self, query: str, limit: int = 200) -> list[dict] | None:
        """Return rows or ``None`` when Cypher is unsupported (Tier 0)."""
        ...
```

`cypher` 返回 `None` 是**能力协商信号**：Tier 0 不支持，Tier 1 支持。
上层据此决定要不要走 Cypher 路径。

- 验证：`uv run python -c "from porto_chatbot.graph import StructureGraph; print('ok')"`

---

## 任务 2：落测试 fixture

- 文件：`backend/tests/fixtures/structure.vg`（新建，二进制）
- 要做的：用附录 A 的 Go 程序生成并复制。**必须由 Go 侧生成**，
  不能用我们的 Python 编码器生成，否则编码器与解码器会互相掩盖 bug。
- 断言基准：5 节点 / 4 边 / indexKeys = `['beanName','conditions','stereotype','type']`，
  含 `bean:orderService ↔ bean:payService` 的 INJECTS 环（via=`lombok-constructor` → fatal）。
- **字节数以实际生成结果为准**（附录 A 含 `loc` 字段，预期约 948 字节，
  不要照抄研究阶段的 929——那是无 `loc` 的旧版）。
- **indexKeys 是字母序**：Vectra 写侧排序（`vectra.go:1873`
  `indexKeys := slices.Sorted(maps.Keys(keySet))`），与附录 A 里
  `IndexProperty` 的调用顺序无关。断言必须用排序后的顺序。
- 验证：`python -c "print(open('backend/tests/fixtures/structure.vg','rb').read()[:4])"` → `b'VCTR'`

---

## 任务 3：RED — 解码器测试

- 文件：`backend/tests/test_vectra_snapshot.py`（新建）
- 要做的：先写测试，**此时 `decode_snapshot` 不存在，测试必须红**。

```python
from pathlib import Path

import pytest

from porto_chatbot.graph.snapshot import SnapshotError, decode_snapshot

_FIX = Path(__file__).parent / "fixtures" / "structure.vg"


def test_decode_returns_all_nodes_and_edges():
    data = decode_snapshot(_FIX.read_bytes())
    assert len(data.nodes) == 5
    assert len(data.edges) == 4
    assert data.index_keys == ["beanName", "conditions", "stereotype", "type"]


def test_decode_hydrates_props_and_labels():
    data = decode_snapshot(_FIX.read_bytes())
    ctrl = next(n for n in data.nodes if n.key == "bean:orderController")
    assert ctrl.label == "Bean"
    assert ctrl.props["stereotype"] == "RestController"


def test_decode_rejects_bad_magic():
    with pytest.raises(SnapshotError, match="magic"):
        decode_snapshot(b"XXXX" + b"\x00" * 32)


def test_decode_rejects_bad_crc():
    buf = bytearray(_FIX.read_bytes())
    buf[-1] ^= 0xFF
    with pytest.raises(SnapshotError, match="crc"):
        decode_snapshot(bytes(buf))


def test_decode_rejects_unsupported_version():
    buf = bytearray(_FIX.read_bytes())
    buf[4:6] = (2).to_bytes(2, "little")
    with pytest.raises(SnapshotError, match="version"):
        decode_snapshot(bytes(buf))


def test_decode_rejects_truncated_payload():
    with pytest.raises(SnapshotError, match="truncated"):
        decode_snapshot(_FIX.read_bytes()[:40])
```

- 验证：`uv run pytest tests/test_vectra_snapshot.py` 全部失败。

---

## 任务 4：GREEN — v3 解码器

- 文件：`backend/src/porto_chatbot/graph/snapshot.py`（新建）
- 要做的：实现 `decode_snapshot(buf: bytes) -> SnapshotData`。格式（小端）：

```
"VCTR" | u16 version(3) | u32 node_count | u32 edge_count
  | node{key, label, props}* | edge{srcKey, dstKey, label, props}*
  | u32 indexKey_count | key string* | u32 CRC32(IEEE)
```

要点：
- CRC 是 **IEEE**（`zlib.crc32`），不是 WAL 的 Castagnoli。
- props 长度 0 = nil → `{}`。
- 每个 `take(n)` 做 `self.i + n > len(self.b)` 检查，越界抛 `SnapshotError("truncated")`。
- **props 惰性解码**：存原始 `bytes`，首次访问才 `json.loads`。实测省 40% 解码耗时。
  用带 `__slots__` 的 `_LazyProps`（`collections.abc.Mapping`）承载，
  保持 `GraphNode.props` 的只读 Mapping 语义。
- 数据类：`SnapshotData(nodes: list[GraphNode], edges: list[GraphEdge],
  index_keys: list[str])`。**`index_keys` 是 `list[str]` 不是 tuple**
  （与 `GraphStats.index_keys` 的 tuple 不同），按文件原样返回，不排序。
- **只支持 v3**：version≠3 一律 `SnapshotError`。这是 Porto 的主动收窄——
  Vectra 自己能读 v1/v2（`store.go:26-27`），但 SCV 只产 v3，
  多支持一套格式就要多一套测试，不划算。

- 验证：`uv run pytest tests/test_vectra_snapshot.py` 全绿。

---

## 任务 5：邻接表 + `neighbors`

- 文件：`backend/src/porto_chatbot/graph/local_graph.py`（新建）
- 要做的：`LocalStructureGraph`，构造时建 `self._out` / `self._in`
  （`dict[str, list[tuple[str, str]]]`，值为 `(other_key, label)`）。
  实现 `node()` / `neighbors()`。`direction` 只接受 `"out"`/`"in"`/`"both"`。
- 验证：断言 `bean:orderController` 出邻居 = `[bean:orderService]`；
  `labels=["CALLS"]` 过滤结果为空。

---

## 任务 6：`bfs`

- 文件：`backend/src/porto_chatbot/graph/local_graph.py`
- 要做的：`bfs(keys, max_depth, labels, limit)` → `[(GraphNode, depth)]`，
  按 `(depth, key)` 排序；起点 depth=0 **不**含在结果里。`limit` 硬截断。
- **默认 labels 必须是 `("INJECTS",)`**。双面加载下不过滤 label 会从 `Bean`
  经 `CONTAINS` 扩散到全仓 `Method` 节点。
- 验证：从 `bean:orderController` depth=2 命中 `bean:payService`；
  `labels=("CALLS",)` 结果为空。

---

## 任务 7：`cycles`（Tarjan + INJECTS 定级）

- 文件：`backend/src/porto_chatbot/graph/local_graph.py`
- 要做的：Tarjan 求 SCC，只保留 size ≥ 2。对每个 SCC 的**内部边**过滤 `INJECTS`：
  - 无 INJECTS 边 → 整环跳过（SCV 语义：CALLS 互递归不算用户可见循环依赖）
  - 有 INJECTS 且任一 `props["via"]` ∈ {`constructor`, `lombok-constructor`, `beanMethod`}
    → `"fatal"`；否则 `"tolerated"`
  - 返回 `[((key, ...), grade)]`
- 依据：`SCV/perception/loader/main.go:45-47, 95-121`。**必须复刻**，
  否则 Porto 会报出与 SCV 不一致的环。
- 验证：fixture 中 `orderService ↔ payService` 判为 `fatal`。

---

## 任务 8：`lookup` + `stats` + Protocol 一致性

- 文件：`backend/src/porto_chatbot/graph/local_graph.py`
- 要做的：`lookup(prop_key, prop_value, limit)` 线性扫描等值匹配（图已在内存）。
  `stats()` 返回 `GraphStats`（SCC/CyclicNodes 由 Tarjan 结果算出，语义对齐
  Vectra：`cyclicNodes` 只数 size>1 的 SCC 成员，不含自环）。
  `cypher()` 返回 `None`（Tier 0 不支持）。
- 验证：断言 `isinstance(graph, StructureGraph)`（比照
  `tests/test_vector_repository_contract.py:239-243`）。

---

# Phase 2 — Tier 1 MCP 通道

## 任务 9：RED — MCP 客户端测试

- 文件：`backend/tests/test_graph_mcp_client.py`（新建）
- 要做的：用 **monkeypatch 一个假的 MCP 服务**（不起真进程，避免 CI 依赖 Go）：

```python
class _FakeProc:
    """Minimal fake MCP server: answers initialize / tools/list / tools/call."""
```

断言：
- `client.cypher("alpha", "MATCH (n) RETURN n")` 返回解析后的 rows
- 未 `initialize` 就调用会先握手
- 服务端返回 `isError` 时抛 `McpError` 且客户端标记为不可用
- 超时抛 `McpError`

- 验证：测试全红。

---

## 任务 10：GREEN — MCP 客户端

- 文件：`backend/src/porto_chatbot/graph/mcp_client.py`（新建）
- 要做的：`McpGraphClient`，JSON-RPC over stdio。协议细节（已实测确认）：

```
→ {"jsonrpc":"2.0","id":N,"method":"initialize",
   "params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{...}}}
→ {"jsonrpc":"2.0","method":"notifications/initialized"}          # 通知，无 id
→ {"jsonrpc":"2.0","id":N,"method":"tools/list","params":{}}
→ {"jsonrpc":"2.0","id":N,"method":"tools/call",
   "params":{"name":"cypher","arguments":{...}}}
← result["result"]["structuredContent"]                            # 直接取，不用解包 text
```

要点：
- **只认 `structuredContent`**；缺失则报错（Vectra 的 Go MCP 一定带）。
- 结果行里 `Node`/`Edge` 已被 `scv-graph-mcp` 转成 plain map（`{key,label,props}`），
  直接可用。
- 每次 `tools/call` 带 `timeout` 参数；超时 → 抛 `McpError` 且置 `self.available = False`。
- `close()` terminate 子进程。

两个后端的 tool 参数差异（**必须分开处理**）：

| | `scv-graph-mcp` | `vectra-mcp` |
|---|---|---|
| 启动 | `[bin, "-analysis-dir", dir]` | `[bin, "-snapshot", path]` |
| cypher | `cypher(repo, query, limit)` | `cypher(query, limit)` |
| get_node | `get_node(repo, key)` | `get_node(key)` |
| 其他 | `repos()`、`refresh()` | `neighbors/bfs/shortest_path/reachable/cycles/stats/lookup_property` |

用 `kind: Literal["scv-graph", "vectra"]` 区分，各自的 cypher 参数装配写成两个分支。

- 验证：`uv run pytest tests/test_graph_mcp_client.py` 全绿。

---

## 任务 11：能力发现与降级

- 文件：`backend/src/porto_chatbot/graph/discovery.py`（新建）
- 要做的：`StructureGraphRegistry`。

```python
class StructureGraphRegistry:
    """Discover ``<kb_dir>/<repo>/structure.vg`` and serve the best available tier."""

    def graph_for(self, repo: str) -> StructureGraph: ...   # 永不返回 None
    def tier(self, repo: str) -> str: ...                   # "mcp" | "local" | "empty"
```

选择顺序：
1. 二进制可用 → `McpGraphClient`（Tier 1）。
   **`scv-graph-mcp` 的发现方式要对齐 SCV 的 `build_graphmcp`**
   （`SCV/perception/perceive.py:323-338`）：
   - 优先 `~/.scv/perception/dist/graphmcp/scv-graph-mcp`
     （可用环境变量 `SCV_PERCEPTION_DIST` 覆盖，`perceive.py:26`）
   - **不要**用 `shutil.which` 找它——SCV 自己也不用
     （`shutil.which` 在 `perceive.py:454` 只用于 `vectra-mcp`）
   - 都找不到且 `settings.graph_mcp_binary` 未指定 → 回退 Tier 0
     （**不要**在运行时 `go build`，那是 SCV 安装期的职责）
2. 否则 → `LocalStructureGraph`（Tier 0），解析 `<kb_dir>/<repo>/structure.vg`。
3. 都没有 → `EmptyStructureGraph`（所有方法返回空值，`cypher` 返回 `None`）。

**三道闸（Tier 0）**：mtime 缓存、节点数 > `graph_max_nodes`（默认 300_000）返回空图、
任何异常 → warning + 空图（不抛）。

- 验证：`tests/test_graph_discovery.py`，`tmp_path` 造
  `<tmp>/kb/alpha/structure.vg`，断言 `graph_for("alpha")` 非空、
  `graph_for("missing")` 是空图且不抛、二次调用命中缓存。

---

# Phase 3 — 集成

## 任务 12：Settings 新字段

- 文件：`backend/src/porto_chatbot/settings.py`
- 要做的：在 `top_k: int = 6`（第 57 行）之后插入：

```python
    # --- SCV Vectra 结构图（第二检索通道）---
    graph_enabled: bool = True
    graph_mcp_enabled: bool = True          # Tier 1；False 则强制走 Tier 0
    graph_mcp_binary: str | None = None     # 显式指定二进制路径；None = PATH 发现
    graph_mcp_timeout: float = Field(default=10.0, ge=1.0)
    graph_max_nodes: int = Field(default=300_000, ge=1)
    graph_max_depth: int = Field(default=2, ge=1, le=5)
    graph_edge_labels: list[str] = Field(default_factory=lambda: ["INJECTS"])
    graph_context_max_nodes: int = Field(default=20, ge=1, le=200)
```

- **同步改 `backend/tests/conftest.py`**：`graph_enabled` 默认 `True`，
  而 `kb_dirs` 默认指向真实的 `~/.scv/analysis`（`settings.py:32`）。
  开发者机器上若真有 `structure.vg`，测试会真的加载几十 MB 的图，
  导致测试变慢且非确定性。新增 autouse fixture：

```python
@pytest.fixture(autouse=True)
def _isolate_kb_dirs(monkeypatch, tmp_path):
    """Point kb_dirs at a temp dir so tests never touch the real ~/.scv."""
    kb = tmp_path / "kb"
    kb.mkdir(exist_ok=True)
    monkeypatch.setenv("PORTO_CHATBOT_KB_DIRS", json.dumps([str(kb)]))
```

  需要图测试用例在自己的 tmp 目录里放 fixture `.vg`，显式 opt-in。
- 验证：`uv run python -c "from porto_chatbot.settings import Settings; s=Settings(); print(s.graph_enabled, s.graph_edge_labels)"` → `True ['INJECTS']`

---

## 任务 13：图上下文渲染

- 文件：`backend/src/porto_chatbot/graph/context.py`（新建）
- 要做的：三个函数。

`build_graph_context(chunks, query, settings, registry) -> list[SourceChunk]`
1. 从 chunks 的 `metadata["root"]` 取仓库名（字段已存在，`vector_store.py:135`）。
2. `registry.graph_for(repo)`。
3. 锚点定位：**不要用 `bm25_index._tokenize_text`**——它在
   `bm25_index.py:6-7` import 了 llama_index，一 import 就破坏 Tier 0
   「零额外依赖」的硬约束，而且它是私有的 CJK 分词函数，语义不匹配标识符。
   在 `graph/context.py` 内自写 ~5 行标识符切分：

```python
def _ident_tokens(text: str) -> set[str]:
    """Split a query into lower-case identifier-ish tokens."""
    return {t for t in re.split(r"[^A-Za-z0-9]+", text.lower()) if len(t) >= 3}
```

   锚点来源优先级：`metadata["root"]` → chunk 的 `title` / `path` 中的标识符
   → query 前 200 字。**不要用完整 query**：`retrieve.py:14` 传进来的是
   `project_name + prd_text[:2000]`，2000 字 PRD 做锚点噪声极大。
4. `bfs(anchors, max_depth=graph_max_depth, labels=graph_edge_labels)`。
5. 渲染 → `SourceChunk(id=f"graph:{repo}", path=f"{repo}/structure.vg",
   title=f"{repo} · 结构依赖", text=..., metadata={"root": repo, "source": "vectra"})`。

`build_identify_summary(settings, registry) -> str`
面向 `identify` 节点的结构摘要。Tier 1 可用时用 Cypher，否则退回 Tier 0：

```
MATCH (n:Bean) RETURN n.stereotype, count(*)                      -- stereotype 普查
MATCH (t:Type) RETURN t.key, t.loc ORDER BY t.loc DESC LIMIT 20    -- 复杂度热点
```
Tier 0 时改为：`lookup("stereotype", ...)` 逐个 stereotype 计数 + `stats()`。

`build_subsystem_impact(settings, sub) -> str`
面向 `generate`：用 `sub.name` + `capabilities` 做锚点匹配 → 影响面 → 渲染。
Tier 1 时用 Cypher 变长边 `MATCH (a)-[:INJECTS*1..2]->(b) RETURN DISTINCT b.key`。

渲染格式（紧凑，供 prompt 消费）：

```
[结构图] repo=<repo>  锚点=bean:orderController  影响面=N 个节点
bean:orderController  Bean  stereotype=RestController  type=com.acme.OrderController
  ──INJECTS(via=constructor)──▶ bean:orderService  Bean  stereotype=Service
⚠ 循环依赖(fatal): bean:orderService → bean:payService → bean:orderService
```

三个函数整体包 `try/except Exception` → `logger.exception` → 返回 `[]` / `""`。
`graph/context.py` 顶部需 `from ..logging_utils import get_component_logger` +
`logger = get_component_logger("graph.context")`。

- 验证：`tests/test_graph_context.py`，断言渲染含锚点与 INJECTS 边；
  断言 registry 返回空图时结果为空且不抛。

---

## 任务 14：注入 `retrieve`

- 文件：`backend/src/porto_chatbot/query_transform.py`
- 要做的：**一共有 3 个返回点，必须全部覆盖**，漏一个就会让降级路径拿不到图：

| 行 | 场景 |
|---|---|
| `query_transform.py:117-123` | NONE 策略（内联 `store.search(...)`，**需先提出为 `rows` 变量**） |
| `query_transform.py:144-147` | LLM 失败的 fail-open 分支（计划 v1 漏了这里） |
| `query_transform.py:149-153` | rerank 之后的正常返回 |

  三处都加：

```python
    rows = _augment_with_graph(rows, query, settings)
```

顶部新增：

```python
def _augment_with_graph(chunks, query, settings):
    """并入 SCV 结构图上下文。任何异常都 fail-open 返回原 chunks。"""
    if not getattr(settings, "graph_enabled", False):
        return chunks
    try:
        from .graph.discovery import StructureGraphRegistry
        from .graph.context import build_graph_context
        return chunks + build_graph_context(
            chunks, query, settings, StructureGraphRegistry(settings))
    except Exception:
        logger.exception("graph context failed → pass-through chunks")
        return chunks
```

- 验证：`uv run pytest tests/ -q` 全绿（无 `.vg` 时行为零变化）；
  断言 `graph_enabled=False` 时结果与今日完全一致。

---

## 任务 15：注入 `identify`（本期收益最大）

- 文件：`backend/src/porto_chatbot/agent/nodes/identify.py`
- 要做的：在 `identify.py:32-35` 的 user prompt 里追加图摘要段落。
  现状该节点输入**只有** `understanding` + PRD 前 2000 字，且 LLM 路径下
  `state["sources"]` 完全没被用到——这是全流水线最弱的一步。

```python
    graph_block = ""
    if getattr(agent.settings, "graph_enabled", False):
        try:
            from ...graph.context import build_identify_summary
            from ...graph.discovery import StructureGraphRegistry
            graph_block = build_identify_summary(
                agent.settings, StructureGraphRegistry(agent.settings))
        except Exception:
            agent.logger.exception("identify graph summary failed → pass-through")
    user = (
        f"业务理解报告:\n{state['understanding']}\n\n"
        f"PRD 节选:\n{prd_text}"
        + (f"\n\n代码结构图摘要:\n{graph_block}" if graph_block else "")
    )
```

摘要内容（Tier 1 有 Cypher 时更丰富）：仓库与规模、SCC/环、`stereotype` 普查、
复杂度热点（`loc` Top 20）。提示子系统边界应当参考强耦合簇（SCC）。

**注意 `logger`**：`identify.py` 没有模块级 logger，只有 `agent.logger`
（该文件 `:21`/`:45`/`:52` 的用法）。用 `agent.logger`，**不要**写裸 `logger`——
否则 `NameError` 会被同一个 `except Exception` 吞掉，图功能永久静默失效。

- 验证：新建 `backend/tests/test_identify_graph.py`（注意：不存在
  `tests/agent/test_identify.py`，不要往那里加）。断言：无图时 prompt
  与今日逐字符一致；有图时 prompt 含「代码结构图摘要」。

---

## 任务 16：注入 `generate`

- 文件：`backend/src/porto_chatbot/specs/steps.py`
- 要做的：在 `steps.py:45-51` 的 system prompt 追加该子系统的影响面摘要。
  `SpecContext` 已有 `settings`（`specs/context.py`，`steps.py:29` 已用
  `ctx.settings.agent_max_tool_turns`），可直接取。

```python
    graph_block = ""
    if getattr(ctx.settings, "graph_enabled", False):
        try:
            graph_block = build_subsystem_impact(ctx.settings, sub)
        except Exception:
            logger.exception("generate graph impact failed → pass-through")
```

**注意 `logger`**：`specs/` 整个包**没有**模块级 logger。必须先加
`from ..logging_utils import get_component_logger` +
`logger = get_component_logger("specs.steps")`，否则 `NameError`
被自己的 `except` 吞掉 → 静默失效。

- 验证：新建 `backend/tests/test_spec_graph_impact.py`（注意：不存在
  `tests/specs/` 目录，spec 相关测试在 `tests/test_spec_loop.py`、
  `tests/test_spec_subgraph.py` 等平铺文件）。断言无图时 system 与今日一致。

---

## 任务 17：新增 `search_code_graph` 工具

- 文件：**共 8 处**，分两组。

**A. 定义与注册（4 处）**
  1. `tools/handlers.py` 新增 `_search_code_graph(ctx, query, top_k=10) -> str`，
     走 Cypher（Tier 1）或 BFS+lookup（Tier 0），结果用
     `_truncate(..., _MAX_TOOL_RESULT_CHARS)` 截断，**只渲染 key + label + 关键 props**。
  2. `tools/registry.py:19` 注册 `ToolDef`（照 `search_knowledgebase` 的 `:50-62` 写法）。
  3. `tools/context.py:37` 的 `AgentToolContext` 加 `graph_registry: Any = None`
     （类定义在 **37 行**，36 行是 `@dataclass` 装饰器；新字段必须带默认值，
     插在末尾安全，因为 `state` 无默认值且排第一）。
  4. `agent_sdk/tools.py:55` 用 `@tool` 装饰器镜像一份（**双后端必须一致**，
     照 `build_sdk_tools` 的 `:128-130` `tools.extend` 范式）。

**B. 构造点（4 处，v1 计划全漏了）**

`AgentToolContext` 加字段后，**每一处构造它的地方都要传**，否则
`ctx.graph_registry` 恒为 `None`，工具永远返回空：

| 文件:行 | 路径 |
|---|---|
| `specs/steps.py:38-42` | generate 子图 |
| `agent/nodes/understand.py:27` | understand 节点 |
| `agent_sdk/backend.py:332` | chatbot 路径 |
| `specs/context.py:22-28` | `SpecContext` 加 `graph_registry: Any = None`，否则 generate 拿不到 |

> 补充：`agent/backends.py:97` 是 `if tools:` 优先、`:106` 是 `if structured_schema:`，
> 二者是 if/elif 优先级关系而非互斥——同时传会**静默忽略 structured**。
> 这让 `identify` 无法「既用工具又出结构化结果」，所以本期 identify 走
> 拼 prompt 路线，不改调度层。
- **约束**：工具自己不能生成 Cypher 交给 LLM 自由执行——Porto 预置查询模板，
  LLM 只传关键词。避免 Cypher 语法错误导致工具报错循环。
- 验证：`tests/test_tools.py` + `tests/test_sdk_tools.py` 断言双后端都有该工具。

---

# Phase 4 — 收尾

## 任务 18：`.env.example` + 文档

- 文件：`backend/.env.example`、`docs/deployment.md`
- 要做的：补 8 个键：

```
PORTO_CHATBOT_GRAPH_ENABLED=true
PORTO_CHATBOT_GRAPH_MCP_ENABLED=true
PORTO_CHATBOT_GRAPH_MCP_BINARY=
PORTO_CHATBOT_GRAPH_MCP_TIMEOUT=10.0
PORTO_CHATBOT_GRAPH_MAX_NODES=300000
PORTO_CHATBOT_GRAPH_MAX_DEPTH=2
PORTO_CHATBOT_GRAPH_EDGE_LABELS=["INJECTS"]
PORTO_CHATBOT_GRAPH_CONTEXT_MAX_NODES=20
```

- **勿照抄 `PORTO_CHATBOT_KB_PATH`**（`.env.example:5`）——该键与 `settings.kb_dirs`
  不匹配，实际不生效。
- 验证：`grep -c PORTO_CHATBOT_GRAPH_ backend/.env.example` → `8`

---

## 任务 19：端到端集成测试

- 文件：`backend/tests/test_vectra_integration.py`（新建）
- 要做的：`tmp_path` 造 SCV 风格目录（`<tmp>/kb/alpha/README.md` +
  `<tmp>/kb/alpha/structure.vg`），走完整
  `retrieve_with_transform(..., QueryTransformStrategy.NONE, ...)`，
  断言返回含 `id="graph:alpha"` 的 chunk。
  再写降级用例：删掉 `structure.vg`，断言不含 graph chunk 且不抛。
- 验证：`uv run pytest tests/test_vectra_integration.py -q` 全绿。

---

## 任务 20（独立，最后做）：rubric 增维「模块覆盖」

- 文件：**「满分 12」被硬编码在 5 处，加维必须全改**，否则会静默截断分数。

| 文件:行 | 现状 | 改成 |
|---|---|---|
| `specs/rubric.py:7` | `# 6 维 × 0-2 分，满分 12` | `7 维 … 满分 14` |
| `specs/rubric.py:9-16` | `SPEC_RUBRIC` 6 维 | 追加第 7 维（见下） |
| `specs/rubric.py:30` | `"maximum": 12` | `"maximum": 14` |
| `specs/steps.py:88` | `"…6 维 rubric … 满分 12：\n"` | `7 维 … 满分 14` |
| `specs/steps.py:90` | `score≥10 … 7-9 … ≤6` | 按 14 分制重算（如 `≥12 / 8-11 / ≤7`） |
| **`specs/steps.py:109`** | **`score = max(0, min(12, score))`** | **`min(14, score)`** ← **最危险：不改这里 13/14 分永远拿不到** |
| `settings.py:88` | `spec_refine_pass_score: int = Field(default=10, ge=0, le=12)` | 上调 default 并放开 `le=14` |

新增维度：

```python
    {"key": "module_coverage", "name": "模块覆盖度",
     "desc": "图影响面中的模块/组件是否在规格中被显式提及或声明依赖"},
```

- 风险：改变 refine loop 基线。**必须最后做、单独验证。**
- 验证：`uv run pytest tests/test_spec_loop.py tests/test_spec_subgraph.py \
  tests/test_spec_loop_tool_truncation.py tests/test_spec_result_tool_meta.py -q` 全绿
  （注意：不存在 `tests/specs/` 目录，spec 测试是平铺的 `tests/test_spec_*.py`）。

---

## 收尾检查

```bash
cd backend
uv run pytest -q
uv run ruff check src tests
grep -rn "inject_batch\|\"seal\"" src/porto_chatbot/graph/    # 必须为空（只读约束）
grep -rn "vectra\|graph" src/porto_chatbot/vector_stores/     # 必须为空（未污染向量层）
```

---

## 附录 A：fixture 生成程序（Go）

```go
// go.mod: module spike / go 1.27
//   require github.com/projanvil/vectra v0.0.0
//   replace github.com/projanvil/vectra => /path/to/Vectra
package main

import "github.com/projanvil/vectra"

func main() {
	g := vectra.New()
	for _, k := range []string{"stereotype", "beanName", "type", "conditions"} {
		g.IndexProperty(k)
	}
	g.AddNode("bean:orderController", "Bean", map[string]any{
		"stereotype": "RestController", "beanName": "orderController",
		"type": "com.acme.OrderController", "source": "OrderController.java:12"})
	g.AddNode("bean:orderService", "Bean", map[string]any{
		"stereotype": "Service", "beanName": "orderService", "type": "com.acme.OrderService"})
	g.AddNode("bean:payService", "Bean", map[string]any{
		"stereotype": "Service", "beanName": "payService", "type": "com.acme.PayService"})
	g.AddNode("type:com.acme.OrderService", "Type", map[string]any{"kind": "class", "loc": 120})
	g.AddNode("fn:com.acme.OrderService#pay(int)", "Method", map[string]any{
		"name": "pay", "owner": "com.acme.OrderService", "loc": 40})
	g.UpsertEdge("bean:orderController", "bean:orderService", "INJECTS",
		map[string]any{"via": "constructor"})
	g.UpsertEdge("bean:orderService", "bean:payService", "INJECTS",
		map[string]any{"via": "lombok-constructor"})
	g.UpsertEdge("bean:payService", "bean:orderService", "INJECTS",
		map[string]any{"via": "field"})
	g.UpsertEdge("type:com.acme.OrderService", "fn:com.acme.OrderService#pay(int)", "CONTAINS", nil)
	if err := g.Seal(); err != nil {
		panic(err)
	}
	if err := g.SaveFile("structure.vg"); err != nil {
		panic(err)
	}
}
```

产出：5 节点 / 4 边。已由 Go `vectra.LoadFile()` + `Cypher()` 交叉验证。

## 附录 B：Cypher 预置查询模板（Porto 侧固定，不由 LLM 生成）

```
-- stereotype 普查（identify 体量画像）
MATCH (n:Bean) RETURN n.stereotype, count(*)

-- 复杂度热点（identify）
MATCH (t:Type) RETURN t.key, t.loc ORDER BY t.loc DESC LIMIT 20

-- 组件影响面（generate）
MATCH (a {key:'bean:<anchor>'})-[:INJECTS*1..2]->(b) RETURN DISTINCT b.key, b.stereotype

-- 分层违规（Controller 直连 Repository）
MATCH (c {stereotype:'RestController'})-[:INJECTS]->(r {stereotype:'Repository'}) RETURN c.key, r.key

-- 接口实现面
MATCH (t:Type)-[:IMPLEMENTS]->(i {kind:'interface'}) RETURN t.key, i.key
```

边界：无 GROUP BY（分组键 = 非聚合 RETURN 项）；变长边不绑边变量；
变长边为最短距离语义；中间行上限 1,000,000。
