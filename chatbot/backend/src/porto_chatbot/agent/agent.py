"""PortoAgent —— Agent 上下文容器。

编排(run/graph/_persist)在 Tasks 5/6 已迁出至 WorkflowRunner/WorkflowExecutor/
WorkflowStore。本模块仅持有 ``settings/llm/vector_store/critic_llm`` 及两个容器级
helper(``_build_critic_llm`` 构造评判模型,``_with_step`` 记录步骤完成日志),供
WorkflowRunner 通过 ``agent`` 参数透传给各 node。
"""

from __future__ import annotations

from typing import Any

from ..llm import LLMClient
from ..logging_utils import get_component_logger
from ..models import AgentStep
from ..settings import Settings
from ..vector_store import LocalVectorStore


class PortoAgent:
    """Agent 上下文容器:持有 settings/llm/vector_store/critic_llm。

    编排由 :class:`porto_chatbot.workflow_runner.WorkflowRunner` 负责(不再有
    ``run``/``graph``/``_persist``)。
    """

    def __init__(
        self,
        settings: Settings,
        vector_store: LocalVectorStore | None = None,
        llm: LLMClient | None = None,
    ):
        self.settings = settings
        self.logger = get_component_logger("agent", settings)
        self.vector_store = vector_store or LocalVectorStore(settings)
        self.llm = llm or LLMClient(settings)
        self.critic_llm = self._build_critic_llm()
        self.logger.info("agent ready")

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

    def _with_step(
        self, state: dict[str, Any], name: str, summary: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        steps = list(state.get("steps", []))
        steps.append(AgentStep(name=name, status="completed", summary=summary, data=data))
        state["steps"] = steps
        self.logger.info(
            "step completed workflow_id=%s name=%s summary=%s",
            state.get("workflow_id"),
            name,
            summary,
        )
        return state
