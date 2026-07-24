from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..config_store import ConfigStore
from ..health import HealthMonitor
from ..index_supervisor import IndexSupervisor
from ..locking import DbLockStore
from ..logging_utils import get_component_logger
from ..memory import MemoryStore
from ..models import AgentSettingsPayload, DocumentSettingsPayload, RagSettingsPayload
from ..vector_store import LocalVectorStore

if TYPE_CHECKING:
    from ..workflow_executor import WorkflowExecutor
    from ..workflow_store import WorkflowStore

logger = get_component_logger("api")


def current_settings():
    """Return the active settings singleton.

    Resolved lazily through the ``porto_chatbot.main`` shim so that tests which
    ``monkeypatch.setattr(main, "settings", ...)`` see their patch applied to
    every dependency factory and route handler. Importing ``main`` at call time
    (rather than at module load time) avoids a circular import.
    """
    from porto_chatbot import main as _main

    return _main.settings


def get_config_store() -> ConfigStore:
    return ConfigStore(current_settings())


def default_rag_settings() -> RagSettingsPayload:
    settings = current_settings()
    return RagSettingsPayload(
        embedding_provider=settings.embedding_provider,
        embedding_model=settings.embedding_model,
        embedding_base_url=settings.embedding_base_url,
        chunk_size=settings.max_chunk_chars,
        chunk_overlap=settings.chunk_overlap,
        top_k=settings.top_k,
        kb_dirs=[str(d) for d in settings.kb_dirs],
        retrieval_method=settings.retrieval_method,
        bm25_top_k=settings.bm25_top_k,
        hybrid_vector_weight=settings.hybrid_vector_weight,
        rerank_enabled=settings.rerank_enabled,
        rerank_top_n=settings.rerank_top_n,
        rerank_provider=settings.rerank_provider,
        rerank_model=settings.rerank_model,
        rerank_choice_batch_size=settings.rerank_choice_batch_size,
    )


def default_agent_settings() -> AgentSettingsPayload:
    settings = current_settings()
    return AgentSettingsPayload(
        agent_provider=settings.agent_provider,
        agent_model=settings.agent_model,
        agent_base_url=settings.agent_base_url,
        agent_api_key=settings.agent_api_key,
        agent_temperature=settings.agent_temperature,
        agent_max_tokens=settings.agent_max_tokens,
        critic_provider=settings.critic_provider,
        critic_model=settings.critic_model,
        critic_base_url=settings.critic_base_url,
        critic_api_key=settings.critic_api_key,
        critic_temperature=settings.critic_temperature,
        critic_max_tokens=settings.critic_max_tokens,
        spec_refine_enabled=settings.spec_refine_enabled,
        spec_refine_max_iter=settings.spec_refine_max_iter,
        spec_refine_concurrency=settings.spec_refine_concurrency,
        spec_refine_pass_score=settings.spec_refine_pass_score,
        spec_refine_budget_tokens=settings.spec_refine_budget_tokens,
        workflow_rework_enabled=settings.workflow_rework_enabled,
        workflow_rework_max_passes=settings.workflow_rework_max_passes,
        memory_compact_threshold=settings.memory_compact_threshold,
        memory_recent_keep=settings.memory_recent_keep,
        context_char_budget=settings.context_char_budget,
        agent_stream_enabled=settings.agent_stream_enabled,
        agent_max_tool_turns=settings.agent_max_tool_turns,
        agent_request_timeout=settings.agent_request_timeout,
    )


def default_document_settings() -> DocumentSettingsPayload:
    settings = current_settings()
    return DocumentSettingsPayload(
        parse_mode=settings.document_parse_mode,
        local_parser=settings.document_local_parser,
        max_tokens=settings.document_max_tokens,
        max_upload_mb=settings.document_max_upload_mb,
        max_pdf_pages=settings.document_max_pdf_pages,
    )


def effective_rag_settings(payload: RagSettingsPayload | None = None) -> RagSettingsPayload:
    updates = default_rag_settings().model_dump(exclude_none=True)
    updates.update(get_config_store().get_rag_settings().model_dump(exclude_none=True))
    if payload:
        updates.update(payload.model_dump(exclude_none=True))
    return RagSettingsPayload(**updates)


def effective_agent_settings(payload: AgentSettingsPayload | None = None) -> AgentSettingsPayload:
    updates = default_agent_settings().model_dump(exclude_none=True)
    updates.update(get_config_store().get_agent_settings().model_dump(exclude_none=True))
    if payload:
        updates.update(payload.model_dump(exclude_none=True))
    return AgentSettingsPayload(**updates)


def effective_document_settings(
    payload: DocumentSettingsPayload | None = None,
) -> DocumentSettingsPayload:
    updates = default_document_settings().model_dump(exclude_none=True)
    updates.update(get_config_store().get_document_settings().model_dump(exclude_none=True))
    if payload:
        updates.update(payload.model_dump(exclude_none=True))
    return DocumentSettingsPayload(**updates)


def apply_rag_settings(
    payload: RagSettingsPayload | None = None,
    agent: AgentSettingsPayload | None = None,
    document: DocumentSettingsPayload | None = None,
    **extra,
):
    settings = current_settings()
    updates = effective_agent_settings(agent).model_dump(exclude_none=True)
    updates.update(effective_rag_settings(payload).model_dump(exclude_none=True))
    document_updates = effective_document_settings(document).model_dump(exclude_none=True)
    updates.update({f"document_{key}": value for key, value in document_updates.items()})
    updates.update(extra)
    if "chunk_size" in updates:
        updates["max_chunk_chars"] = updates.pop("chunk_size")
    if "kb_dirs" in updates and updates["kb_dirs"] is not None:
        updates["kb_dirs"] = [Path(d) for d in updates["kb_dirs"]]
    return settings.model_copy(update=updates)


def get_store(runtime_settings=None) -> LocalVectorStore:
    return LocalVectorStore(runtime_settings or current_settings())


def get_memory(runtime_settings=None) -> MemoryStore:
    return MemoryStore(runtime_settings or current_settings())


# ---------------- RAG 监督 / 健康监控 单例 ----------------
# 按 data_dir 缓存：data_dir 变化（如测试用 tmp_path）时自动重建，保证隔离。

_rag_singletons: dict = {}


def _ensure_rag_singletons() -> dict:
    settings = current_settings()
    key = str(settings.data_dir)
    entry = _rag_singletons.get(key)
    if entry is not None:
        return entry
    lock_store = DbLockStore(settings)
    supervisor = IndexSupervisor(
        lock_store=lock_store,
        store_factory=lambda s: LocalVectorStore(s),
        # RAG 可用性判断必须用「实际生效」配置（db 覆盖 .env），否则 collection metadata
        # （按 db 配置 build）与 current_settings（.env）的 embedding_model 不匹配，
        # _is_collection_compatible 误判 → rag_available 恒返回 index_unavailable。
        settings_provider=apply_rag_settings,
    )
    health = HealthMonitor(
        # 探测必须用「实际生效」的配置（db 覆盖 .env），而非 current_settings（纯 .env）。
        # 否则用户在 UI 改的 embedding 模型 / agent key 不生效，面板显示 .env 旧值的探测结果。
        settings_provider=apply_rag_settings,
        rag_available=supervisor.rag_available,
        rag_status=supervisor.get_status,
    )
    entry = {"lock": lock_store, "supervisor": supervisor, "health": health}
    _rag_singletons[key] = entry
    return entry


def get_lock_store() -> DbLockStore:
    return _ensure_rag_singletons()["lock"]


def get_index_supervisor() -> IndexSupervisor:
    return _ensure_rag_singletons()["supervisor"]


def get_health_monitor() -> HealthMonitor:
    return _ensure_rag_singletons()["health"]


def get_workflow_store() -> WorkflowStore:
    """按 data_dir 缓存的 WorkflowStore 单例(懒加载,挂入现有 entry dict)。

    复用 ``_ensure_rag_singletons`` 的 data_dir-keyed entry,避免另起一套并行的
    单例系统;同一测试的 data_dir 内 store/executor 与其他 rag 单例共享生命周期。
    """
    from ..workflow_store import WorkflowStore

    entry = _ensure_rag_singletons()
    store = entry.get("workflow_store")
    if store is None:
        store = WorkflowStore(current_settings())
        entry["workflow_store"] = store
    return store


def get_checkpointer():
    """按 data_dir 缓存的 langgraph SqliteSaver 单例(独立于 workflows.sqlite3)。

    U3(L1 spike)已确认 SqliteSaver 自带 threading.Lock 序列化 SQLite 访问,
    多 daemon 线程共享同一 connection(check_same_thread=False)安全。
    """
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    entry = _ensure_rag_singletons()
    cp = entry.get("checkpointer")
    if cp is None:
        settings = current_settings()
        db_path = settings.data_dir / "langgraph_checkpoints.sqlite"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        cp = SqliteSaver(conn)
        cp.setup()
        entry["checkpointer"] = cp
        entry["_checkpoint_conn"] = conn
    return cp


def get_workflow_graph():
    """按 data_dir 缓存的编译后 workflow graph 单例(挂共享 checkpointer)。"""
    from ..agent.graph import build_workflow_graph

    entry = _ensure_rag_singletons()
    g = entry.get("workflow_graph")
    if g is None:
        g = build_workflow_graph(get_checkpointer())
        entry["workflow_graph"] = g
    return g


def get_workflow_executor() -> WorkflowExecutor:
    """按 data_dir 缓存的 WorkflowExecutor 单例(懒加载),注入共享 graph。"""
    from ..workflow_executor import WorkflowExecutor

    entry = _ensure_rag_singletons()
    ex = entry.get("workflow_executor")
    if ex is None:
        ex = WorkflowExecutor(current_settings(), get_workflow_store(), get_workflow_graph())
        entry["workflow_executor"] = ex
    return ex


def reset_rag_singletons() -> None:
    """测试用：停止并清空单例，保证每个测试的 data_dir 隔离。"""
    global _rag_singletons
    for entry in _rag_singletons.values():
        try:
            entry["supervisor"].stop()
        except Exception:
            pass
        try:
            entry["health"].stop()
        except Exception:
            pass
        try:
            # Conditional close: if a workflow worker (and its langgraph-internal
            # ThreadPoolExecutor) is still mid-invoke on this conn, closing would
            # race with in-flight checkpoint put writes (C-level sqlite3 op) and
            # SIGSEGV the interpreter. ``try/except`` cannot catch that, so we
            # gate on the executor's per-workflow guard locks; any locked guard
            # means a worker may still be using the conn → leak it (the sqlite
            # file is in the test's tmp_path and will be reaped by pytest). The
            # TOCTOU window (worker starts after the check) does not arise in
            # tests because no new requests arrive during fixture teardown.
            ex = entry.get("workflow_executor")
            if ex is not None:
                with ex._global:
                    if any(lk.locked() for lk in ex._guards.values()):
                        continue  # worker still running; leak conn to avoid SIGSEGV
            entry["_checkpoint_conn"].close()
        except Exception:
            pass
    _rag_singletons = {}


def runtime_settings_from_snapshot(
    rag_snapshot: dict, agent_snapshot: dict, top_k: int | None = None
):
    """从创建时的 resolved 配置快照重建 Settings,**不读 db**(免受后续配置改动影响)。

    snapshot 是创建时 effective_rag_settings/agent_settings 的 model_dump;此处以
    Settings() 默认(.env)为底,用 snapshot 完整覆盖。这样在后台线程跑 workflow 时,
    即使用户后续在 UI 改了配置,本 workflow 仍按创建时的快照执行。

    与 :func:`apply_rag_settings` 的区别:后者每次调用都读 db 合并最新配置,适合
    ad-hoc 请求;本函数纯靠 snapshot,适合长生命周期的工作流。
    """
    settings = current_settings()
    updates = {**agent_snapshot, **rag_snapshot}
    if top_k is not None:
        updates["top_k"] = top_k
    if "chunk_size" in updates:
        updates["max_chunk_chars"] = updates.pop("chunk_size")
    if updates.get("kb_dirs"):
        updates["kb_dirs"] = [Path(d) for d in updates["kb_dirs"]]
    return settings.model_copy(update={k: v for k, v in updates.items() if v is not None})
