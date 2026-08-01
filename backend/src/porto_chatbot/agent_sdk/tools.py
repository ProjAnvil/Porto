# backend/src/porto_chatbot/agent_sdk/tools.py
"""Build @tool-decorated functions that wrap existing handlers.py logic.

Each @tool binds the current AgentToolContext via closure. The actual logic
lives in handlers.py — zero duplication. The SDK is imported lazily so this
module can be imported even when ``claude_agent_sdk`` is not installed; in
that case ``build_sdk_tools`` returns ``[]`` and the caller handles the
fallback gracefully.
"""
from __future__ import annotations

from ..memory.facts import build_facts_prompt
from ..tools.context import AgentToolContext
from ..tools.handlers import (
    _get_prd_text,
    _get_sources,
    _get_subsystem,
    _get_understanding,
    _list_subsystems,
    _search_knowledgebase,
)


def _mcp_text(text: str) -> dict:
    """Return Agent SDK MCP content-block format (single text block)."""
    return {"content": [{"type": "text", "text": text}]}


def build_sdk_tools(ctx: AgentToolContext) -> list:
    """Create ``@tool``-decorated functions bound to ``ctx`` via closure.

    - Workflow ctx (``memory_store is None``): registers the 6 core tools
      (``get_prd_text``, ``get_understanding``, ``list_subsystems``,
      ``get_subsystem``, ``search_knowledgebase``, ``get_sources``).
    - Chatbot ctx (``memory_store``/``facts_store`` present): additionally
      registers ``search_memory`` and ``get_session_facts``.

    Returns ``[]`` when ``claude_agent_sdk`` is not importable, so callers can
    feature-detect and fall back to another backend without crashing.
    """
    try:
        from claude_agent_sdk import tool
    except ImportError:
        return []  # SDK not installed — caller handles gracefully

    tools: list = []

    # ------------------------------------------------------------------ #
    # Core workflow tools (always registered)
    # ------------------------------------------------------------------ #
    @tool("get_prd_text", "读取当前 PRD 原文。当需要回顾输入需求时调用。", {})
    async def get_prd(args):  # noqa: ANN001
        return _mcp_text(_get_prd_text(ctx))

    @tool(
        "get_understanding",
        "读取已生成的业务理解报告（understand_prd 节点产物）。",
        {},
    )
    async def get_understanding(args):  # noqa: ANN001
        return _mcp_text(_get_understanding(ctx))

    @tool("list_subsystems", "列出已识别的子系统及其职责。", {})
    async def list_subs(args):  # noqa: ANN001
        return _mcp_text(_list_subsystems(ctx))

    @tool(
        "get_subsystem",
        "按名称读取单个子系统的完整定义（职责、能力、数据实体、依赖）。",
        {"name": str},
    )
    async def get_sub(args):  # noqa: ANN001
        return _mcp_text(_get_subsystem(ctx, str(args.get("name", ""))))

    @tool(
        "search_knowledgebase",
        "在知识库中检索与 query 相关的文档片段。top_k 控制返回条数，默认 6。",
        {"query": str, "top_k": int},
    )
    async def search_kb(args):  # noqa: ANN001
        top_k = int(args.get("top_k", 6) or 6)
        return _mcp_text(
            _search_knowledgebase(ctx, str(args.get("query", "")), top_k)
        )

    @tool(
        "get_sources",
        "读取已检索到的知识库片段（retrieve_knowledge 节点产物或之前 tool 检索结果）。"
        "可选按 query 过滤。",
        {"query": str},
    )
    async def get_srcs(args):  # noqa: ANN001
        return _mcp_text(_get_sources(ctx, str(args.get("query", ""))))

    tools.extend(
        [get_prd, get_understanding, list_subs, get_sub, search_kb, get_srcs]
    )

    # ------------------------------------------------------------------ #
    # Chatbot-specific tools (only when memory_store/facts_store present)
    # ------------------------------------------------------------------ #
    if ctx.memory_store is not None:
        @tool(
            "search_memory",
            "跨会话语义检索对话记忆。session_id 可选，用于限定检索范围。",
            {"query": str, "session_id": str},
        )
        async def search_mem(args):  # noqa: ANN001
            results = ctx.memory_store.search(
                str(args.get("query", "")),
                session_id=str(args.get("session_id", "")) or None,
            )
            from ..tools.context import _format_chunks

            ctx.state.setdefault("tool_memory", []).extend(results)
            return _mcp_text(
                _format_chunks(results) if results else "无匹配记忆。"
            )

        tools.append(search_mem)

    if ctx.facts_store is not None:
        @tool(
            "get_session_facts",
            "读取本会话的结构化关键事实（决策/偏好/背景/待澄清），供 system prompt 参考。",
            {"session_id": str},
        )
        async def get_facts(args):  # noqa: ANN001
            grouped = ctx.facts_store.by_category(str(args.get("session_id", "")))
            text = build_facts_prompt(grouped)
            return _mcp_text(text if text else "当前会话无结构化事实。")

        tools.append(get_facts)

    return tools
