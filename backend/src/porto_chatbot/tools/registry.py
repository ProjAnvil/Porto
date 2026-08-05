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
    _read_file_info,
    _read_file_pages,
    _search_file,
    _search_knowledgebase,
)


def build_agent_tools(ctx: AgentToolContext) -> list[ToolDef]:
    """构造节点内可用的工具集。"""
    tools: list[ToolDef] = [
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

    if ctx.file_service is not None:
        tools.extend(
            [
                ToolDef(
                    name="get_file_info",
                    description=(
                        "读取已上传文件的元信息（原始文件名、页数、大小、MIME 类型）。"
                        "file_id 来自用户消息或 state.prd_file_id。"
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "file_id": {
                                "type": "string",
                                "description": "目标文件 ID",
                            },
                        },
                        "required": ["file_id"],
                    },
                    handler=lambda args: _read_file_info(
                        ctx, str(args.get("file_id", ""))
                    ),
                ),
                ToolDef(
                    name="read_file_pages",
                    description=(
                        "读取已上传文件指定页码范围的文本（1-based, inclusive）。"
                        "先调用 get_file_info 获取页数，再按需取页。"
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "file_id": {
                                "type": "string",
                                "description": "目标文件 ID",
                            },
                            "start": {
                                "type": "integer",
                                "description": "起始页（1-based, inclusive）",
                            },
                            "end": {
                                "type": "integer",
                                "description": "结束页（1-based, inclusive）",
                            },
                        },
                        "required": ["file_id", "start", "end"],
                    },
                    handler=lambda args: _read_file_pages(
                        ctx,
                        str(args.get("file_id", "")),
                        int(args.get("start", 1) or 1),
                        int(args.get("end", 1) or 1),
                    ),
                ),
                ToolDef(
                    name="search_file",
                    description=(
                        "在已上传文件内做大小写不敏感的子串搜索，返回所有命中页码与上下文片段。"
                        "适合先定位关键内容再 read_file_pages 取完整页。"
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "file_id": {
                                "type": "string",
                                "description": "目标文件 ID",
                            },
                            "query": {
                                "type": "string",
                                "description": "搜索关键词",
                            },
                        },
                        "required": ["file_id", "query"],
                    },
                    handler=lambda args: _search_file(
                        ctx,
                        str(args.get("file_id", "")),
                        str(args.get("query", "")),
                    ),
                ),
            ]
        )

    return tools
