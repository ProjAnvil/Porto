"""PortoAgent —— Agent 上下文容器。

编排(graph/_persist)已迁出至 WorkflowExecutor/WorkflowStore/langgraph StateGraph。
本模块仅持有 ``settings/llm/vector_store/critic_llm`` 及两个容器级
helper(``_build_critic_llm`` 构造评判模型,``_step`` 记录步骤完成日志),供
各节点经 ``config["configurable"]["agent"]`` 透传消费。
"""

from __future__ import annotations

import threading
from typing import Any

from ..llm import LLMClient
from ..logging_utils import get_component_logger
from ..models import AgentStep
from ..models.enums import StepStatus
from ..settings import Settings
from ..vector_store import LocalVectorStore


class PortoAgent:
    """Agent 上下文容器:持有 settings/llm/vector_store/critic_llm。

    编排由 langgraph StateGraph(``agent.graph.build_workflow_graph``)+
    :class:`porto_chatbot.workflow_executor.WorkflowExecutor` 负责(不再有
    ``run``/``_persist``)。
    """

    def __init__(
        self,
        settings: Settings,
        vector_store: LocalVectorStore | None = None,
        llm: LLMClient | None = None,
        *,
        file_service: Any = None,
    ):
        """``file_service`` 由 workflow_executor._build_agent 注入(FileService 实例)。

        默认 None:节点经 ``getattr(agent, "file_service", None)`` 取到 None 时
        回退到 ``prd_file_id``/``prd_text`` 内联文本路径(供单元测试 / text 路由使用)。
        """
        self.settings = settings
        self.logger = get_component_logger("agent", settings)
        self.vector_store = vector_store or LocalVectorStore(settings)
        self.llm = llm or LLMClient(settings)
        self.file_service = file_service
        self.critic_llm = self._build_critic_llm()
        from .factory import BackendScope, create_backend
        self.backend = create_backend(settings, llm=self.llm, scope=BackendScope.WORKFLOW)
        # M3: Send fan-out 并发限流 —— dispatch_specs 注入各子图实例，init_spec 入口 acquire。
        # LangGraph 同步执行模型下不会产生真并发（顺序 fan-out），语义上限制同时活跃的
        # spec 子图实例数；若后续切到 async/pool 执行器则生效为真实信号量。
        self._spec_sema = threading.Semaphore(settings.spec_refine_concurrency)
        self.logger.info(
            "agent ready backend=%s file_service=%s",
            type(self.backend).__name__,
            type(file_service).__name__ if file_service else "(none)",
        )

    def _build_critic_llm(self) -> LLMClient:
        """构造 spec loop 的评判模型。未配 critic_* 时回退到 generator(``self.llm``)。

        用 setattr 而非 model_copy:BaseSettings 字段带 validation_alias 时,
        model_copy 的 update 对 env 已有值的字段会被重新加载的 env 值覆盖
        (pydantic-settings 行为)。setattr 则稳定生效。
        """
        s = self.settings
        if not s.critic_provider:
            return self.llm
        critic_settings = Settings()
        critic_settings.agent_provider = s.critic_provider
        critic_settings.agent_api_key = s.critic_api_key or s.agent_api_key
        critic_settings.agent_base_url = s.critic_base_url or s.agent_base_url
        critic_settings.agent_model = s.critic_model or s.agent_model
        critic_settings.agent_temperature = s.critic_temperature
        critic_settings.agent_max_tokens = s.critic_max_tokens
        critic = LLMClient(critic_settings)
        self.logger.info(
            "critic llm ready provider=%s model=%s independent=%s",
            s.critic_provider,
            s.critic_model,
            critic.enabled,
        )
        return critic

    def _step(self, name: str, summary: str, data: dict[str, Any]) -> dict[str, Any]:
        """返回 partial 更新 ``{"steps": [AgentStep(...)]}`` + 记完成日志。

        节点把它 spread 进自己的返回值(steps 走 ``operator.add`` reducer 追加)。
        """
        self.logger.info("step completed name=%s summary=%s", name, summary)
        return {"steps": [AgentStep(name=name, status=StepStatus.COMPLETED, summary=summary, data=data)]}
