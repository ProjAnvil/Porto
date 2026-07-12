"""构造节点内可用的工具集（build_agent_tools）。"""
from __future__ import annotations

from ..llm import ToolDef
from .context import AgentToolContext
from .handlers import (
    _get_prd_text,
    _get_sources,
    _get_subsystem,
    _get_understanding,
    _list_subsystems,
    _search_knowledgebase,
)


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
