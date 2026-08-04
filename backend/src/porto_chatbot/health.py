from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime

import ollama
from anthropic import Anthropic
from openai import OpenAI

from .logging_utils import get_component_logger
from .models import (
    DependencyHealth,
    DependencyName,
    DependencyStatus,
    EmbeddingProvider,
    FeatureAvailability,
    FeatureName,
    HealthSnapshot,
    LLMProvider,
)
from .settings import Settings

logger = get_component_logger("health")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class HealthMonitor:
    """依赖级 + 功能级健康监控。

    daemon 线程按 ``health_probe_interval`` 周期探测 embedding / agent LLM / critic LLM
    的连通性，缓存最近一次快照。功能级可用度由「依赖态 × RAG 状态」在快照时推导。
    探测本身不触发 reindex，RAG 可用性读取自 :class:`IndexSupervisor`。
    """

    def __init__(
        self,
        settings_provider: Callable[[], Settings],
        rag_available: Callable[[], tuple[bool, str | None]],
        rag_status: Callable,
    ):
        self._settings_provider = settings_provider
        self._rag_available = rag_available
        self._rag_status = rag_status
        self._snapshot = HealthSnapshot()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="health-probe")

    # ---------------- 生命周期 ----------------
    def start(self) -> None:
        self.probe_all()  # 首次立即探测，避免启动后只有 unknown
        self._thread = threading.Thread(target=self._run, name="health-monitor", daemon=True)
        self._thread.start()
        logger.info("health monitor started")

    def stop(self) -> None:
        self._stop.set()
        self._executor.shutdown(wait=False, cancel_futures=True)
        logger.info("health monitor stop requested")

    def snapshot(self) -> HealthSnapshot:
        return self._snapshot

    # ---------------- 探测主循环 ----------------
    def _run(self) -> None:
        while not self._stop.wait(self._settings_provider().health_probe_interval):
            try:
                self.probe_all()
            except Exception:
                logger.exception("health probe cycle failed")

    def probe_all(self) -> HealthSnapshot:
        settings = self._settings_provider()
        deps: list[DependencyHealth] = [
            self._probe_embedding(settings),
            self._probe_llm(settings, DependencyName.AGENT_LLM),
            self._probe_llm(settings, DependencyName.CRITIC_LLM),
        ]
        features = self._derive_features(deps)
        self._snapshot = HealthSnapshot(
            dependencies=deps,
            features=features,
            rag_index=self._rag_status(),
            updated_at=_now_iso(),
        )
        logger.info(
            "health probe done deps=%s",
            {d.name: d.status for d in deps},
        )
        return self._snapshot

    # ---------------- 依赖探测 ----------------
    def _probe_embedding(self, settings: Settings) -> DependencyHealth:
        name = DependencyName.EMBEDDING
        if settings.embedding_provider == EmbeddingProvider.LOCAL:
            return DependencyHealth(
                name=name, status=DependencyStatus.OK, detail="local", checked_at=_now_iso()
            )
        try:
            latency = self._executor.submit(
                self._ollama_ping, settings.embedding_base_url, settings.embedding_model
            ).result(timeout=settings.health_probe_timeout)
            return DependencyHealth(
                name=name, status=DependencyStatus.OK, latency_ms=latency,
                detail=f"{settings.embedding_model}@{settings.embedding_base_url}",
                checked_at=_now_iso(),
            )
        except FutureTimeout:
            return DependencyHealth(
                name=name, status=DependencyStatus.DOWN,
                detail=f"timeout >{settings.health_probe_timeout}s",
                checked_at=_now_iso(),
            )
        except Exception as exc:
            return DependencyHealth(name=name, status=DependencyStatus.DOWN, detail=_short(exc), checked_at=_now_iso())

    def _probe_llm(self, settings: Settings, name: DependencyName) -> DependencyHealth:
        provider, api_key, base_url, model = self._resolve_llm_config(settings, name)
        inherits = name == DependencyName.CRITIC_LLM and not settings.critic_provider
        if not api_key:
            return DependencyHealth(
                name=name, status=DependencyStatus.UNKNOWN, detail="no api key", checked_at=_now_iso()
            )
        try:
            latency = self._executor.submit(
                self._llm_ping, provider, api_key, base_url, model
            ).result(timeout=settings.health_probe_timeout)
            tag = "inherits agent" if inherits else f"{model}@{provider}"
            return DependencyHealth(
                name=name, status=DependencyStatus.OK, latency_ms=latency, detail=tag, checked_at=_now_iso()
            )
        except FutureTimeout:
            return DependencyHealth(
                name=name, status=DependencyStatus.DOWN, detail=f"timeout >{settings.health_probe_timeout}s",
                checked_at=_now_iso(),
            )
        except Exception as exc:
            return DependencyHealth(name=name, status=DependencyStatus.DOWN, detail=_short(exc), checked_at=_now_iso())

    @staticmethod
    def _resolve_llm_config(settings: Settings, name: DependencyName):
        if name == DependencyName.AGENT_LLM:
            return (
                settings.agent_provider, settings.agent_api_key,
                settings.agent_base_url, settings.agent_model,
            )
        # critic 回退到 agent 配置
        return (
            settings.critic_provider or settings.agent_provider,
            settings.critic_api_key or settings.agent_api_key,
            settings.critic_base_url or settings.agent_base_url,
            settings.critic_model or settings.agent_model,
        )

    @staticmethod
    def _ollama_ping(base_url: str, model: str) -> float:
        client = ollama.Client(host=base_url)
        t0 = time.perf_counter()
        client.embed(model=model, input="ping")
        return round((time.perf_counter() - t0) * 1000, 1)

    @staticmethod
    def _llm_ping(provider: str, api_key: str, base_url: str | None, model: str) -> float:
        t0 = time.perf_counter()
        if provider == LLMProvider.OPENAI:
            kwargs: dict = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            OpenAI(**kwargs).chat.completions.create(
                model=model, messages=[{"role": "user", "content": "ping"}], max_tokens=1,
            )
        elif provider == LLMProvider.ANTHROPIC:
            kwargs = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            Anthropic(**kwargs).messages.create(
                model=model, max_tokens=1, messages=[{"role": "user", "content": "ping"}],
            )
        else:
            raise ValueError(f"unsupported provider: {provider}")
        return round((time.perf_counter() - t0) * 1000, 1)

    # ---------------- 功能级推导 ----------------
    def _derive_features(self, deps: list[DependencyHealth]) -> list[FeatureAvailability]:
        by_name = {d.name: d for d in deps}
        agent_dep = by_name.get(DependencyName.AGENT_LLM)
        embed_dep = by_name.get(DependencyName.EMBEDDING)
        agent_ok = agent_dep.status != DependencyStatus.DOWN if agent_dep else False
        embed_ok = embed_dep.status == DependencyStatus.OK if embed_dep else False
        rag_avail, rag_reason = self._rag_available()

        features: list[FeatureAvailability] = []
        features.append(
            FeatureAvailability(
                name=FeatureName.CHAT, available=agent_ok,
                reason=None if agent_ok else "agent_llm_down",
            )
        )
        if not rag_avail:
            features.append(FeatureAvailability(name=FeatureName.RAG_SEARCH, available=False, reason=rag_reason))
        elif not embed_ok:
            features.append(FeatureAvailability(name=FeatureName.RAG_SEARCH, available=False, reason="embedding_down"))
        else:
            features.append(FeatureAvailability(name=FeatureName.RAG_SEARCH, available=True))
        if not agent_ok:
            features.append(FeatureAvailability(name=FeatureName.WORKFLOW, available=False, reason="agent_llm_down"))
        elif not rag_avail:
            features.append(FeatureAvailability(name=FeatureName.WORKFLOW, available=False, reason=rag_reason))
        else:
            features.append(FeatureAvailability(name=FeatureName.WORKFLOW, available=True))
        return features


def _short(exc: Exception, limit: int = 200) -> str:
    return (str(exc) or exc.__class__.__name__)[:limit]
