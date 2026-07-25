from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from .locking import RAG_INDEX_LOCK, DbLockStore
from .logging_utils import get_component_logger
from .models import IndexJobStatus
from .settings import Settings
from .vector_store import ChromaVectorStore

logger = get_component_logger("index_supervisor")


@dataclass
class _IndexRequest:
    settings: Settings
    reset: bool
    source: str


class IndexSupervisor:
    """唯一的 RAG reindex 执行者。

    - 单 worker daemon 线程串行执行 build，从根上消除并发 reset 互删。
    - ``submit`` 时若 worker 正忙 → 直接拒绝（返回当前 running 状态），**不入队**；
      用户需等当前任务完成后再次触发。
    - build 过程通过 ``progress_cb`` 把文档级进度/心跳回写到 ``service_locks``。
    - 不做启动恢复重建；``start()`` 仅把上次进程崩溃残留的 ``running`` 就地归位为
      ``interrupted``，避免 reindex 永远卡在 busy。
    """

    def __init__(
        self,
        lock_store: DbLockStore,
        store_factory: Callable[[Settings], ChromaVectorStore],
        settings_provider: Callable[[], Settings],
    ):
        self._lock_store = lock_store
        self._store_factory = store_factory
        self._settings_provider = settings_provider
        self._current: _IndexRequest | None = None
        self._submit_lock = threading.Lock()
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------- 生命周期 ----------------
    def start(self) -> None:
        """启动 worker。先清理上次崩溃残留（不重建），再起 daemon 线程。"""
        self._lock_store.mark_interrupted(RAG_INDEX_LOCK, error="interrupted by restart")
        self._thread = threading.Thread(target=self._run, name="index-supervisor", daemon=True)
        self._thread.start()
        logger.info("index supervisor started")

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        # F4: join worker —— teardown 时后台 reindex 必须结束,否则 worker 继续操作
        # collection(reset 重建),与下个测试的 collection 操作竞争 → chromadb TOCTOU
        # NotFoundError(test_list_and_delete ~20% flake)。
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=10.0)
        logger.info("index supervisor stop requested")

    # ---------------- 提交 ----------------
    def submit(
        self,
        settings: Settings,
        *,
        reset: bool = True,
        source: str = "manual",
    ) -> IndexJobStatus:
        """提交一次 reindex。worker 正忙时拒绝（返回当前 running 状态），不排队。"""
        with self._submit_lock:
            if self._current is not None:
                logger.info("submit rejected busy incoming_source=%s", source)
                return self._lock_store.get_status()
            self._current = _IndexRequest(settings=settings, reset=reset, source=source)
            # 立即占位 running，让前端马上看到状态（worker 接手后由 progress_cb 刷新）
            self._lock_store.mark_running(RAG_INDEX_LOCK, source=source, reset=reset, total=0)
        with self._cv:
            self._cv.notify()
        logger.info("submit accepted source=%s reset=%s", source, reset)
        return self._lock_store.get_status()

    def get_status(self) -> IndexJobStatus:
        return self._lock_store.get_status()

    # ---------------- RAG 可用性 ----------------
    def rag_available(self) -> tuple[bool, str | None]:
        """RAG 检索是否可用：不在 reindex 中且索引就绪。返回 (available, reason)。"""
        status = self._lock_store.get_status()
        if status.status == "running":
            return False, "reindexing"
        try:
            store = self._store_factory(self._settings_provider())
            ready = store.is_rag_ready()
        except Exception:
            logger.exception("rag availability check failed")
            ready = False
        return (True, None) if ready else (False, "index_unavailable")

    # ---------------- worker ----------------
    def _run(self) -> None:
        while not self._stop.is_set():
            req = self._wait_for_request()
            if req is None:
                continue
            try:
                self._execute(req)
            except Exception:
                logger.exception("index worker crashed")
                self._lock_store.mark_failed(RAG_INDEX_LOCK, error="worker crashed")
            finally:
                with self._submit_lock:
                    self._current = None
                with self._cv:
                    self._cv.notify_all()

    def _wait_for_request(self) -> _IndexRequest | None:
        with self._cv:
            while not self._stop.is_set() and self._current is None:
                self._cv.wait(timeout=1.0)
            return self._current

    def _execute(self, req: _IndexRequest) -> None:
        store = self._store_factory(req.settings)

        def progress_cb(done: int, total: int, chunks_done: int) -> None:
            self._lock_store.update_progress(
                RAG_INDEX_LOCK, done=done, total=total, chunks_done=chunks_done
            )

        stats = store.build(reset=req.reset, progress_cb=progress_cb)
        self._lock_store.mark_succeeded(RAG_INDEX_LOCK, stats=stats)
        logger.info(
            "index job done source=%s documents=%s chunks=%s",
            req.source,
            stats.documents,
            stats.chunks,
        )
