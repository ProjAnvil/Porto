"""Render structure-graph context for prompts and retrieval results.

Every entry point here is total and fail-open: a missing binary, an unreadable
snapshot, a broken registry or an unexpected exception all produce an empty
result rather than an error, because graph context is an enrichment of the
existing retrieval path — never a precondition for it.

The two rendering targets differ in what they need:

* :func:`build_graph_context` answers "what does this retrieval hit touch", and
  returns a :class:`SourceChunk` so the result flows through the same surfaces
  as any other retrieved chunk.
* :func:`build_identify_summary` and :func:`build_subsystem_impact` answer "what
  does this repository look like" and "what does this subsystem touch", and
  return plain text for direct prompt assembly.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..logging_utils import get_component_logger
from ..models import SourceChunk
from .discovery import SNAPSHOT_NAME, StructureGraphRegistry

logger = get_component_logger("graph.context")

# The retrieve node searches with project_name plus the first 2000 chars of the PRD;
# using all of it for anchors is mostly noise, so only a short prefix is taken.
_ANCHOR_QUERY_CHARS = 200
_PROP_KEYS = ("stereotype", "type", "beanName", "kind", "owner", "loc", "source")
_MAX_PROPS = 4
_SURFACE_PREFIXES = ("bean", "type", "fn", "file")
_CENSUS_LIMIT = 200
_REPO_LIMIT = 5


def build_graph_context(
    chunks: list[SourceChunk],
    query: str,
    settings: Any,
    registry: Any,
) -> list[SourceChunk]:
    """Render the graph impact surface behind retrieved chunks.

    Parameters
    ----------
    chunks:
        Retrieved chunks; ``metadata["root"]`` selects the repository.
    query:
        The retrieval query, truncated before use as an anchor source.
    settings:
        Any object exposing ``graph_enabled``, ``graph_max_depth``,
        ``graph_edge_labels`` and ``graph_context_max_nodes``.
    registry:
        A :class:`StructureGraphRegistry`, or anything with ``graph_for``.

    Returns
    -------
    list[SourceChunk]
        One chunk per repository that had a usable graph, or ``[]``.
    """
    if not getattr(settings, "graph_enabled", False):
        return []
    roots = _roots(chunks)
    if not roots:
        return []
    try:
        labels = tuple(getattr(settings, "graph_edge_labels", None) or ())
        depth = int(getattr(settings, "graph_max_depth", 2) or 2)
        limit = int(getattr(settings, "graph_context_max_nodes", 20) or 20)
        out: list[SourceChunk] = []
        for repo in roots:
            rendered = _render_repo(
                registry, repo, _anchor_text(query, chunks, repo), depth, labels or None, limit
            )
            if rendered:
                out.append(
                    SourceChunk(
                        id=f"graph:{repo}",
                        path=f"{repo}/{SNAPSHOT_NAME}",
                        title=f"{repo} · 结构依赖",
                        text=rendered,
                        metadata={"root": repo, "source": "vectra"},
                    )
                )
        return out
    except Exception:
        logger.exception("graph context failed → empty")
        return []


def build_identify_summary(settings: Any, registry: Any) -> str:
    """Render a whole-repository structure summary for the identify step.

    Reports size, dependency cycles, the component stereotype census and the
    largest types — the signals that say where subsystem boundaries actually
    sit. Cycles are reported first because a strongly connected cluster is a
    natural single subsystem.

    Parameters
    ----------
    settings:
        Any object exposing ``graph_enabled`` and ``graph_max_nodes``.
    registry:
        A :class:`StructureGraphRegistry`, or anything with ``graph_for``.

    Returns
    -------
    str
        Rendered summary, or ``""`` when no graph is usable.
    """
    if not getattr(settings, "graph_enabled", False):
        return ""
    try:
        blocks: list[str] = []
        for repo in _repos(settings)[:_REPO_LIMIT]:
            graph = registry.graph_for(repo)
            stats = graph.stats()
            if not stats.nodes:
                continue
            lines = [
                f"仓库 {repo}: {stats.nodes} 节点 / {stats.edges} 边 "
                f"/ {stats.sccs} 个强连通分量 / {stats.cyclic_nodes} 个环内节点"
            ]
            for members, grade in graph.cycles()[:3]:
                lines.append(f"  循环依赖({grade}): {' → '.join(members)}")
            census = _census(graph)
            if census:
                lines.append("  组件构成: " + "、".join(census))
            hot = _hotspots(graph)
            if hot:
                lines.append("  规模热点: " + "、".join(hot))
            blocks.append("\n".join(lines))
        return "\n".join(blocks)
    except Exception:
        logger.exception("identify graph summary failed → empty")
        return ""


def build_subsystem_impact(settings: Any, sub: Any) -> str:
    """Render the impact surface of one subsystem for the generate step.

    Parameters
    ----------
    settings:
        Any object exposing the ``graph_*`` settings.
    sub:
        A :class:`Subsystem`; ``name`` and ``capabilities`` seed the anchors.

    Returns
    -------
    str
        Rendered impact surface, or ``""`` when no graph is usable.
    """
    if not getattr(settings, "graph_enabled", False):
        return ""
    try:
        registry = StructureGraphRegistry(settings)
        anchors_text = " ".join(
            [str(getattr(sub, "name", "") or "")]
            + [str(c) for c in (getattr(sub, "capabilities", None) or [])]
        )
        blocks: list[str] = []
        for repo in _repos(settings)[:_REPO_LIMIT]:
            rendered = _render_repo(
                registry, repo, anchors_text,
                int(getattr(settings, "graph_max_depth", 2) or 2),
                tuple(getattr(settings, "graph_edge_labels", None) or ()) or None,
                int(getattr(settings, "graph_context_max_nodes", 20) or 20),
            )
            if rendered:
                blocks.append(rendered)
        return "\n".join(blocks)
    except Exception:
        logger.exception("subsystem graph impact failed → empty")
        return ""


# --- rendering ------------------------------------------------------------ #


def _render_repo(
    registry: Any,
    repo: str,
    anchor_text: str,
    depth: int,
    labels: tuple[str, ...] | None,
    limit: int,
) -> str:
    """Render one repository's impact surface, or ``""`` when unusable."""
    graph = registry.graph_for(repo)
    if not graph.stats().nodes:
        return ""
    anchors = [n.key for n in graph.find(anchor_text, limit=5)]
    if not anchors:
        return ""
    surface = graph.bfs(anchors, max_depth=depth, labels=labels, limit=limit)
    wanted = labels or ("INJECTS",)
    lines = [
        f"[结构图] repo={repo}  锚点={'、'.join(anchors)}  "
        f"影响面={len(surface)} 个节点（按 {'/'.join(wanted)} 传播，深度 1-{depth}）"
    ]
    for node, hop in surface:
        lines.append(f"  depth{hop}: {node.key}  {node.label}  {_props(node)}")
    for members, grade in graph.cycles()[:3]:
        lines.append(f"⚠ 循环依赖({grade}): {' → '.join(members)}")
    return "\n".join(lines)


def _props(node: Any) -> str:
    """Render the few node properties worth putting in a prompt."""
    props = getattr(node, "props", None) or {}
    parts = [f"{k}={props[k]}" for k in _PROP_KEYS if k in props]
    return "  ".join(parts[:_MAX_PROPS])


def _census(graph: Any) -> list[str]:
    """Tally Bean nodes by stereotype, most common first."""
    tally: dict[str, int] = {}
    for node in graph.find("bean", limit=_CENSUS_LIMIT):
        stereotype = (getattr(node, "props", None) or {}).get("stereotype")
        if stereotype:
            tally[str(stereotype)] = tally.get(str(stereotype), 0) + 1
    ranked = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
    return [f"{name}×{count}" for name, count in ranked[:6]]


def _hotspots(graph: Any) -> list[str]:
    """Largest types by ``loc`` — where the complexity actually sits."""
    sized = [
        (int(props["loc"]), node.key)
        for node in graph.find("type", limit=_CENSUS_LIMIT)
        if (props := (getattr(node, "props", None) or {})).get("loc") is not None
    ]
    sized.sort(key=lambda pair: (-pair[0], pair[1]))
    return [f"{key}({loc} 行)" for loc, key in sized[:5]]


# --- inputs --------------------------------------------------------------- #


def _roots(chunks: list[SourceChunk]) -> list[str]:
    """Repository names carried by the chunks, de-duplicated in order."""
    roots: list[str] = []
    for chunk in chunks:
        root = (getattr(chunk, "metadata", None) or {}).get("root")
        if isinstance(root, str) and root and root not in roots:
            roots.append(root)
    return roots


def _anchor_text(query: str, chunks: list[SourceChunk], repo: str) -> str:
    """Text used to locate anchors: chunk titles first, query only as a tail.

    The query is PRD prose; titles and paths carry the identifiers the graph
    keys are made of. Tokens shorter than three characters are dropped.
    """
    parts = [repo]
    for chunk in chunks:
        if (getattr(chunk, "metadata", None) or {}).get("root") != repo:
            continue
        parts.append(str(getattr(chunk, "title", "") or ""))
        parts.append(Path(str(getattr(chunk, "path", "") or "")).stem)
    parts.append(str(query or "")[:_ANCHOR_QUERY_CHARS])
    return " ".join(parts)


def _repos(settings: Any) -> list[str]:
    """Repositories under the knowledge-base directories that have a snapshot."""
    dirs = getattr(settings, "kb_dirs", None) or []
    names: list[str] = []
    for directory in dirs:
        try:
            base = Path(directory).expanduser()
            if not base.is_dir():
                continue
            for child in sorted(base.iterdir()):
                if child.is_dir() and (child / SNAPSHOT_NAME).is_file():
                    if child.name not in names:
                        names.append(child.name)
        except OSError:
            continue
    return names
