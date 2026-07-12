"""节点内 tool calling 的工具集。

每个节点内的 LLM 不是单次补全，而是带 tools 的 mini agent loop（见 LLMClient.complete_with_tools）。
工具通过 AgentToolContext 访问 workflow state 与向量库，把取数决策交给 LLM。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .llm import ToolDef
from .models import SourceChunk
from .vector_store import LocalVectorStore

State = dict[str, Any]

# 单个 tool 结果的字符上限，避免把 context 撑爆
_MAX_PRD_CHARS = 6000
_MAX_SOURCE_CHARS = 800
_MAX_TOOL_RESULT_CHARS = 6000


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，共 {len(text)} 字）"


def _format_chunks(chunks: list[SourceChunk], *, per_chunk: int = _MAX_SOURCE_CHARS) -> str:
    if not chunks:
        return "无匹配片段。"
    lines = []
    for i, c in enumerate(chunks, 1):
        lines.append(f"[{i}] {c.path} score={c.score}\n{_truncate(c.text, per_chunk)}")
    return "\n\n".join(lines)


@dataclass
class AgentToolContext:
    """工具执行上下文：持有可变的 workflow state 与向量库句柄。"""

    state: State
    vector_store: LocalVectorStore | None = None


def _get_prd_text(ctx: AgentToolContext) -> str:
    prd = str(ctx.state.get("prd_text", "")).strip()
    return _truncate(prd, _MAX_PRD_CHARS) if prd else "PRD 文本尚未提供。"


def _get_understanding(ctx: AgentToolContext) -> str:
    text = str(ctx.state.get("understanding", "")).strip()
    return text if text else "业务理解报告尚未生成（当前节点可能位于 understand_prd 之前）。"


def _list_subsystems(ctx: AgentToolContext) -> str:
    subsystems = ctx.state.get("subsystems") or []
    if not subsystems:
        return "子系统列表尚未生成。"
    lines = []
    for s in subsystems:
        name = s.get("name") if isinstance(s, dict) else getattr(s, "name", "")
        resp = s.get("responsibility") if isinstance(s, dict) else getattr(s, "responsibility", "")
        lines.append(f"- {name}: {resp}")
    return f"已识别 {len(subsystems)} 个子系统：\n" + "\n".join(lines)


def _get_subsystem(ctx: AgentToolContext, name: str) -> str:
    subsystems = ctx.state.get("subsystems") or []
    for s in subsystems:
        s_name = s.get("name") if isinstance(s, dict) else getattr(s, "name", "")
        if s_name == name:
            caps = s.get("capabilities") if isinstance(s, dict) else getattr(s, "capabilities", [])
            ents = s.get("data_entities") if isinstance(s, dict) else getattr(s, "data_entities", [])
            deps = s.get("dependencies") if isinstance(s, dict) else getattr(s, "dependencies", [])
            return (
                f"子系统：{s_name}\n"
                f"类型：{s.get('type') if isinstance(s, dict) else getattr(s, 'type', '')}\n"
                f"职责：{s.get('responsibility') if isinstance(s, dict) else getattr(s, 'responsibility', '')}\n"
                f"能力：{', '.join(caps)}\n"
                f"数据实体：{', '.join(ents)}\n"
                f"依赖：{', '.join(deps)}"
            )
    return f"未找到名为 {name!r} 的子系统。"


def _search_knowledgebase(ctx: AgentToolContext, query: str, top_k: int = 6) -> str:
    if ctx.vector_store is None:
        return "知识库不可用（vector_store 未初始化）。"
    results = ctx.vector_store.search(query, top_k=top_k)
    ctx.state.setdefault("tool_sources", []).extend(results)
    return _truncate(_format_chunks(results), _MAX_TOOL_RESULT_CHARS)


def _get_sources(ctx: AgentToolContext, query: str = "") -> str:
    """返回已检索到的知识库片段（retrieve_knowledge 节点产物或之前 tool 检索结果）。"""
    sources = ctx.state.get("sources") or []
    if query:
        query_lower = query.lower()
        filtered = [
            s for s in sources
            if query_lower in (getattr(s, "text", "") or "").lower()
            or query_lower in (getattr(s, "path", "") or "").lower()
        ]
        sources = filtered or sources
    return _truncate(_format_chunks(sources), _MAX_TOOL_RESULT_CHARS) if sources else "尚无已检索的知识库片段。"


def build_agent_tools(ctx: AgentToolContext) -> list[ToolDef]:
    """构造节点内可用的工具集。"""
    return [
        ToolDef(
            name="get_prd_text",
            description="读取当前 PRD 原文。无需参数。当需要回顾输入需求时调用。",
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=lambda args: _get_prd_text(ctx),
        ),
        ToolDef(
            name="get_understanding",
            description="读取已生成的业务理解报告（Step 1 产物）。无需参数。",
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=lambda args: _get_understanding(ctx),
        ),
        ToolDef(
            name="list_subsystems",
            description="列出已识别的子系统及其职责。无需参数。",
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=lambda args: _list_subsystems(ctx),
        ),
        ToolDef(
            name="get_subsystem",
            description="按名称读取单个子系统的完整定义（类型/职责/能力/数据实体/依赖）。",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "子系统名称，如 payment-service"}},
                "required": ["name"],
            },
            handler=lambda args: _get_subsystem(ctx, str(args.get("name", ""))),
        ),
        ToolDef(
            name="search_knowledgebase",
            description="在知识库中检索与查询相关的文档片段。用于参考现有系统模式与约定。",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索查询"},
                    "top_k": {"type": "integer", "description": "返回片段数，默认 6", "default": 6},
                },
                "required": ["query"],
            },
            handler=lambda args: _search_knowledgebase(ctx, str(args.get("query", "")), int(args.get("top_k", 6) or 6)),
        ),
        ToolDef(
            name="get_sources",
            description="读取已检索到的知识库片段（retrieve_knowledge 节点产物）。可选按 query 过滤。",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "可选过滤词"}},
                "required": [],
            },
            handler=lambda args: _get_sources(ctx, str(args.get("query", ""))),
        ),
    ]
