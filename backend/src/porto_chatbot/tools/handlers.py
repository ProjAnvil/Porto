"""工具 handler 实现。

每个 handler 接收 AgentToolContext，访问 workflow state 与向量库以获取数据。
"""
from __future__ import annotations

from .context import (
    _MAX_PRD_CHARS,
    _MAX_TOOL_RESULT_CHARS,
    AgentToolContext,
    _format_chunks,
    _truncate,
)


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


# --- File tools -------------------------------------------------------------


def _require_file_service(ctx: AgentToolContext):
    """返回注入的 FileService，未注入时抛 RuntimeError。"""
    if ctx.file_service is None:
        raise RuntimeError("file_service 未注入")
    return ctx.file_service


def _read_file_info(ctx: AgentToolContext, file_id: str) -> str:
    """读取文件元信息（名称/页数/大小/类型）。"""
    info = _require_file_service(ctx).get_info(file_id)
    if info is None:
        return f"[错误] 文件 {file_id} 不存在"
    return (
        f"文件: {info.original_name}\n"
        f"页数: {info.page_count}\n"
        f"大小: {info.size_bytes}\n"
        f"类型: {info.mime}"
    )


def _read_file_pages(
    ctx: AgentToolContext, file_id: str, start: int, end: int
) -> str:
    """读取指定页码范围（1-based, inclusive）的文本。"""
    text = _require_file_service(ctx).read_pages(file_id, start, end)
    return _truncate(text, _MAX_TOOL_RESULT_CHARS)


def _search_file(ctx: AgentToolContext, file_id: str, query: str) -> str:
    """在指定文件内按 query 做大小写不敏感子串搜索。"""
    hits = _require_file_service(ctx).search(file_id, query)
    if not hits:
        return f"未找到 '{query}'"
    return "\n".join(f"第 {h.page} 页: {h.snippet}" for h in hits)
