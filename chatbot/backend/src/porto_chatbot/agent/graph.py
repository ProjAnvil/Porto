from __future__ import annotations

import json
import uuid
from typing import Any

from langgraph.graph import END, StateGraph

from ..llm import LLMClient
from ..logging_utils import get_component_logger
from ..models import AgentStep, WorkflowResponse
from ..settings import Settings
from ..vector_store import LocalVectorStore
from .heuristics import infer_project_name
from .nodes import evaluate as evaluate_node
from .nodes import generate as generate_node
from .nodes import identify as identify_node
from .nodes import retrieve as retrieve_node
from .nodes import understand as understand_node
from .state import PortoAgentState


class PortoAgent:
    """固定 workflow 的编排器：retrieve → understand → identify → generate → evaluate，
    evaluate 不达标时按 workflow_rework_max_passes 条件回边到 identify_subsystems 重做。"""

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
        self.graph = self._build_graph()
        self.logger.info("agent ready")

    def _build_critic_llm(self) -> LLMClient:
        """构造 spec loop 的评判模型。未配 critic_* 时回退到 generator（self.llm）。"""
        s = self.settings
        if not s.critic_provider:
            return self.llm
        critic_settings = s.model_copy(update={
            "agent_provider": s.critic_provider,
            "agent_api_key": s.critic_api_key or s.agent_api_key,
            "agent_base_url": s.critic_base_url,
            "agent_model": s.critic_model or s.agent_model,
            "agent_temperature": s.critic_temperature,
            "agent_max_tokens": s.critic_max_tokens,
        })
        critic = LLMClient(critic_settings)
        self.logger.info(
            "critic llm ready provider=%s model=%s independent=%s",
            s.critic_provider, s.critic_model, critic.enabled,
        )
        return critic

    def run(self, prd_text: str, project_name: str | None = None, top_k: int | None = None) -> WorkflowResponse:
        workflow_id = str(uuid.uuid4())
        self.logger.info(
            "workflow run start workflow_id=%s project_name=%s prd_chars=%s top_k=%s",
            workflow_id,
            project_name,
            len(prd_text),
            top_k,
        )
        initial: PortoAgentState = {
            "workflow_id": workflow_id,
            "project_name": project_name or infer_project_name(prd_text),
            "prd_text": prd_text.strip(),
            "steps": [],
            "top_k": top_k,
        }
        result = self.graph.invoke(initial)
        response = WorkflowResponse(
            workflow_id=result["workflow_id"],
            project_name=result["project_name"],
            understanding=result["understanding"],
            subsystems=result["subsystems"],
            specs=result["specs"],
            evaluation=result["evaluation"],
            sources=result["sources"],
            steps=result["steps"],
        )
        self._persist(response)
        self.logger.info(
            "workflow run finish workflow_id=%s project_name=%s subsystems=%s score=%s",
            response.workflow_id,
            response.project_name,
            len(response.subsystems),
            response.evaluation.get("score"),
        )
        return response

    def retrieve_knowledge(self, state: PortoAgentState) -> PortoAgentState:
        return retrieve_node.retrieve_knowledge(self, state)

    def understand_prd(self, state: PortoAgentState) -> PortoAgentState:
        return understand_node.understand_prd(self, state)

    def identify_subsystems(self, state: PortoAgentState) -> PortoAgentState:
        return identify_node.identify_subsystems(self, state)

    def generate_specs(self, state: PortoAgentState) -> PortoAgentState:
        return generate_node.generate_specs(self, state)

    def evaluate(self, state: PortoAgentState) -> PortoAgentState:
        return evaluate_node.evaluate(self, state)

    def _build_graph(self):
        graph = StateGraph(PortoAgentState)
        graph.add_node("retrieve_knowledge", self.retrieve_knowledge)
        graph.add_node("understand_prd", self.understand_prd)
        graph.add_node("identify_subsystems", self.identify_subsystems)
        graph.add_node("generate_specs", self.generate_specs)
        graph.add_node("evaluate", self.evaluate)
        graph.set_entry_point("retrieve_knowledge")
        graph.add_edge("retrieve_knowledge", "understand_prd")
        graph.add_edge("understand_prd", "identify_subsystems")
        graph.add_edge("identify_subsystems", "generate_specs")
        graph.add_edge("generate_specs", "evaluate")
        graph.add_conditional_edges(
            "evaluate",
            self._route_after_evaluate,
            {"identify_subsystems": "identify_subsystems", END: END},
        )
        return graph.compile()

    def _route_after_evaluate(self, state: PortoAgentState) -> str:
        """不达标且未超重做上限 → 回 identify_subsystems 重做；否则结束。"""
        if state.get("needs_rework"):
            self.logger.info(
                "workflow rework workflow_id=%s pass=%s",
                state.get("workflow_id"),
                state.get("rework_passes", 0),
            )
            return "identify_subsystems"
        return END

    def _persist(self, response: WorkflowResponse) -> None:
        workflow_dir = self.settings.workflows_dir / response.workflow_id
        workflow_dir.mkdir(parents=True, exist_ok=True)
        (workflow_dir / "workflow.json").write_text(
            response.model_dump_json(indent=2), encoding="utf-8"
        )
        (workflow_dir / "step1_understanding.md").write_text(response.understanding, encoding="utf-8")
        (workflow_dir / "step2_subsystems.json").write_text(
            json.dumps([s.model_dump() for s in response.subsystems], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        step4_dir = workflow_dir / "step4"
        step4_dir.mkdir(exist_ok=True)
        for name, spec in response.specs.items():
            subdir = step4_dir / name
            subdir.mkdir(exist_ok=True)
            (subdir / "REQUIREMENTS.md").write_text(spec, encoding="utf-8")
        self.logger.info("workflow persisted workflow_id=%s dir=%s", response.workflow_id, workflow_dir)

    def _with_step(
        self, state: PortoAgentState, name: str, summary: str, data: dict[str, Any]
    ) -> PortoAgentState:
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
