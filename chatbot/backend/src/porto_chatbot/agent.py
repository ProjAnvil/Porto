from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from .llm import LLMClient
from .logging_utils import get_component_logger
from .models import AgentStep, SourceChunk, SpecResult, Subsystem, WorkflowResponse
from .settings import Settings
from .specs import SpecContext, generate_spec_with_loop
from .tools import AgentToolContext, build_agent_tools
from .vector_store import LocalVectorStore


class PortoAgentState(TypedDict, total=False):
    workflow_id: str
    project_name: str
    prd_text: str
    sources: list[SourceChunk]
    understanding: str
    subsystems: list[Subsystem]
    specs: dict[str, str]
    spec_results: dict[str, SpecResult]
    evaluation: dict[str, Any]
    steps: list[AgentStep]
    top_k: int | None
    rework_passes: int
    needs_rework: bool


DOMAIN_HINTS = {
    "user": ["用户", "账户", "认证", "登录", "权限", "profile", "account", "auth"],
    "order": ["订单", "下单", "履约", "交易", "order", "checkout"],
    "payment": ["支付", "收款", "退款", "结算", "payment", "refund", "settlement"],
    "notification": ["通知", "短信", "邮件", "站内信", "notification", "message"],
    "catalog": ["商品", "库存", "目录", "sku", "catalog", "inventory"],
    "risk": ["风控", "风险", "反欺诈", "审核", "risk", "fraud"],
    "reporting": ["报表", "统计", "分析", "dashboard", "report"],
}


class PortoAgent:
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
        self.graph = self._build_graph()
        self.logger.info("agent ready")

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
            "project_name": project_name or self._infer_project_name(prd_text),
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

    def retrieve_knowledge(self, state: PortoAgentState) -> PortoAgentState:
        self.logger.info("step retrieve_knowledge start workflow_id=%s", state["workflow_id"])
        self.vector_store.ensure_index()
        query = f"{state['project_name']}\n{state['prd_text'][:2000]}"
        sources = self.vector_store.search(query, top_k=state.get("top_k"))
        self.logger.info(
            "step retrieve_knowledge finish workflow_id=%s sources=%s",
            state["workflow_id"],
            len(sources),
        )
        return self._with_step(
            {**state, "sources": sources},
            "retrieve_knowledge",
            f"检索到 {len(sources)} 个知识库片段",
            {"source_paths": [s.path for s in sources]},
        )

    def understand_prd(self, state: PortoAgentState) -> PortoAgentState:
        self.logger.info("step understand_prd start workflow_id=%s", state["workflow_id"])
        understanding = ""
        if self.llm.enabled:
            ctx = AgentToolContext(state=state, vector_store=self.vector_store)
            result = self.llm.complete_with_tools(
                "你是资深业务分析师。根据 PRD 和知识库片段，输出简洁的中文业务理解报告，"
                "包含：执行摘要、业务目标、核心实体、子系统线索。"
                "可调用工具获取 PRD 原文与检索知识库，自主决定检索什么。",
                "请生成业务理解报告。",
                build_agent_tools(ctx),
            )
            understanding = (result.text or "").strip()
            self.logger.info(
                "step understand_prd llm tool_calls=%s turns=%s chars=%s",
                len(result.tool_calls),
                result.turns,
                len(understanding),
            )
        if not understanding:
            understanding = self._fallback_understanding(state)
            self.logger.info("step understand_prd used fallback workflow_id=%s", state["workflow_id"])
        return self._with_step(
            {**state, "understanding": understanding},
            "understand_prd",
            "完成业务理解报告",
            {"chars": len(understanding), "used_llm": bool(understanding) and self.llm.enabled},
        )

    def _fallback_understanding(self, state: PortoAgentState) -> str:
        text = state["prd_text"]
        goals = self._extract_bullets(text, ["目标", "需要", "实现", "支持", "管理"])
        entities = self._extract_entities(text)
        return "\n".join(
            [
                "# Step 1: 业务需求理解",
                "",
                f"## 1. 执行摘要\n{self._summary_sentence(text)}",
                "",
                "## 2. 业务目标",
                *(f"- {g}" for g in goals[:6]),
                "",
                "## 3. 核心实体",
                *(f"- {e}" for e in entities[:12]),
                "",
                "## 4. 子系统线索",
                *(f"- {name}-service: {', '.join(hints[:3])}" for name, hints in self._matched_domains(text).items()),
            ]
        )

    def identify_subsystems(self, state: PortoAgentState) -> PortoAgentState:
        self.logger.info("step identify_subsystems start workflow_id=%s", state["workflow_id"])
        subsystems: list[Subsystem] = []
        if self.llm.enabled:
            parsed = self.llm.complete_structured(
                "你是资深系统架构师。按领域驱动设计原则，根据业务理解报告与 PRD 识别需要拆分的子系统。"
                "每个子系统职责单一、边界清晰，数量控制在 2-6 个，命名形如 xxx-service。",
                f"业务理解报告:\n{state['understanding']}\n\nPRD 节选:\n{state['prd_text'][:2000]}",
                self._subsystem_schema(),
            )
            raw_list = (parsed or {}).get("subsystems", []) if isinstance(parsed, dict) else []
            for raw in raw_list:
                norm = self._normalize_sub_dict(raw)
                if norm:
                    subsystems.append(Subsystem(**norm))
            self.logger.info(
                "step identify_subsystems llm parsed=%s accepted=%s",
                len(raw_list),
                len(subsystems),
            )
        if not subsystems:
            subsystems = self._fallback_identify(state)
            self.logger.info("step identify_subsystems used fallback workflow_id=%s", state["workflow_id"])
        return self._with_step(
            {**state, "subsystems": subsystems},
            "identify_subsystems",
            f"识别 {len(subsystems)} 个子系统",
            {
                "subsystems": [s.model_dump() for s in subsystems],
                "used_llm": bool(subsystems) and self.llm.enabled,
            },
        )

    def _subsystem_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "subsystems": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "子系统名称，形如 xxx-service"},
                            "type": {"type": "string", "enum": ["new", "extend", "existing"]},
                            "responsibility": {"type": "string"},
                            "capabilities": {"type": "array", "items": {"type": "string"}},
                            "data_entities": {"type": "array", "items": {"type": "string"}},
                            "dependencies": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["name", "responsibility"],
                    },
                }
            },
            "required": ["subsystems"],
        }

    def _normalize_sub_dict(self, d: object) -> dict | None:
        """把 LLM 输出的子系统 dict 安全归一化为 Subsystem 构造参数。"""
        if not isinstance(d, dict):
            return None
        name = str(d.get("name", "")).strip()
        if not name:
            return None
        raw_type = d.get("type", "new")
        return {
            "name": name,
            "type": raw_type if raw_type in ("new", "extend", "existing") else "new",
            "responsibility": str(d.get("responsibility", "")).strip() or "（LLM 未给出职责）",
            "capabilities": [str(c) for c in (d.get("capabilities") or [])][:12],
            "data_entities": [str(e) for e in (d.get("data_entities") or [])][:12],
            "dependencies": [str(dep) for dep in (d.get("dependencies") or [])][:12],
        }

    def _fallback_identify(self, state: PortoAgentState) -> list[Subsystem]:
        domains = self._matched_domains(state["prd_text"] + "\n" + state["understanding"])
        subsystems: list[Subsystem] = []
        for domain, matches in domains.items():
            name = f"{domain}-service"
            sources_text = " ".join(s.text for s in state.get("sources", []))
            subsystem_type = "extend" if domain in sources_text.lower() or name in sources_text.lower() else "new"
            subsystems.append(
                Subsystem(
                    name=name,
                    type=subsystem_type,
                    responsibility=self._responsibility_for(domain),
                    capabilities=self._capabilities_for(domain, matches),
                    data_entities=self._entities_for(domain),
                    dependencies=[],
                )
            )
        if not subsystems:
            subsystems = [
                Subsystem(
                    name="core-service",
                    type="new",
                    responsibility="承载核心业务流程和领域规则",
                    capabilities=["需求管理", "业务流程编排", "状态追踪"],
                    data_entities=["Requirement", "Workflow"],
                )
            ]
        return subsystems

    def generate_specs(self, state: PortoAgentState) -> PortoAgentState:
        self.logger.info(
            "step generate_specs start workflow_id=%s subsystems=%s",
            state["workflow_id"],
            len(state["subsystems"]),
        )
        subs = state["subsystems"]

        def _gen(sub: Subsystem) -> SpecResult:
            # 浅拷贝 state：并行时各子任务 tool 检索写各自的 tool_sources，互不干扰
            sub_ctx = SpecContext(
                llm=self.llm,
                state={**state},
                settings=self.settings,
                vector_store=self.vector_store,
            )
            return generate_spec_with_loop(sub_ctx, sub)

        results: dict[str, SpecResult] = {}
        parallel = (
            self.settings.spec_refine_parallel
            and self.llm.enabled
            and self.settings.spec_refine_enabled
            and len(subs) > 1
        )
        if parallel:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=min(8, len(subs))) as pool:
                futures = {pool.submit(_gen, sub): sub for sub in subs}
                for future, sub in futures.items():
                    results[sub.name] = future.result()
        else:
            for sub in subs:
                results[sub.name] = _gen(sub)

        specs = {name: result.final for name, result in results.items()}
        used_llm = any(r.used_llm for r in results.values())
        total_iters = sum(r.iterations for r in results.values())
        self.logger.info(
            "step generate_specs finish specs=%s used_llm=%s iterations=%s",
            len(specs),
            used_llm,
            total_iters,
        )
        return self._with_step(
            {**state, "specs": specs, "spec_results": results},
            "generate_specs",
            f"生成 {len(specs)} 份子系统规格",
            {
                "spec_names": list(specs),
                "used_llm": used_llm,
                "iterations": total_iters,
                "attempts": {name: r.model_dump() for name, r in results.items()},
            },
        )

    def evaluate(self, state: PortoAgentState) -> PortoAgentState:
        from .evaluation import evaluate_workflow

        self.logger.info("step evaluate start workflow_id=%s", state["workflow_id"])
        evaluation = evaluate_workflow(
            state["prd_text"],
            state["understanding"],
            state["subsystems"],
            state["specs"],
        )
        # 聚合 spec loop 的 rubric 分数（若启用 LLM loop）
        spec_results = state.get("spec_results") or {}
        rubric_scores = [r.attempts[-1].score for r in spec_results.values() if r.attempts]
        if rubric_scores:
            evaluation["spec_rubric_avg"] = round(sum(rubric_scores) / len(rubric_scores), 2)
            evaluation["spec_rubric_min"] = min(rubric_scores)

        # 条件回边决策
        passes = int(state.get("rework_passes", 0))
        below_bar = (
            not evaluation.get("passed", True)
            or evaluation.get("spec_rubric_min", self.settings.spec_refine_pass_score)
            < self.settings.spec_refine_pass_score
        )
        needs_rework = (
            self.settings.workflow_rework_enabled
            and below_bar
            and passes < self.settings.workflow_rework_max_passes
        )
        self.logger.info(
            "step evaluate finish score=%s rubric_avg=%s needs_rework=%s passes=%s",
            evaluation.get("score"),
            evaluation.get("spec_rubric_avg"),
            needs_rework,
            passes,
        )
        return self._with_step(
            {
                **state,
                "evaluation": evaluation,
                "rework_passes": passes + 1 if needs_rework else passes,
                "needs_rework": needs_rework,
            },
            "evaluate",
            f"评估得分 {evaluation['score']}",
            evaluation,
        )

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

    def _infer_project_name(self, text: str) -> str:
        first = next((line.strip("# ：:") for line in text.splitlines() if line.strip()), "")
        return first[:40] or f"Porto 项目 {datetime.now(UTC).strftime('%Y%m%d%H%M')}"

    def _summary_sentence(self, text: str) -> str:
        clean = re.sub(r"\s+", " ", text).strip()
        return clean[:180] + ("..." if len(clean) > 180 else "")

    def _extract_bullets(self, text: str, keywords: list[str]) -> list[str]:
        lines = [re.sub(r"^[-*#\d.、\s]+", "", line).strip() for line in text.splitlines()]
        matches = [line for line in lines if line and any(k.lower() in line.lower() for k in keywords)]
        return matches or [self._summary_sentence(text)]

    def _extract_entities(self, text: str) -> list[str]:
        candidates = re.findall(r"[\u4e00-\u9fffA-Za-z]{2,}(?:用户|订单|支付|账户|商品|通知|规则|任务|报表|服务|系统|记录)", text)
        seen = []
        for item in candidates:
            if item not in seen:
                seen.append(item)
        return seen or ["用户", "业务流程", "需求记录"]

    def _matched_domains(self, text: str) -> dict[str, list[str]]:
        lower = text.lower()
        result: dict[str, list[str]] = {}
        for domain, hints in DOMAIN_HINTS.items():
            found = [h for h in hints if h.lower() in lower]
            if found:
                result[domain] = found
        return result

    def _responsibility_for(self, domain: str) -> str:
        return {
            "user": "负责用户身份、账户资料和权限边界",
            "order": "负责订单生命周期、交易状态和履约协同",
            "payment": "负责支付、退款、结算和资金通道集成",
            "notification": "负责消息模板、通知投递和触达记录",
            "catalog": "负责商品目录、库存快照和 SKU 信息",
            "risk": "负责风险识别、规则决策和审核流转",
            "reporting": "负责指标聚合、报表查询和运营分析",
        }.get(domain, "负责核心业务能力")

    def _capabilities_for(self, domain: str, matches: list[str]) -> list[str]:
        labels = {
            "user": ["注册登录", "权限校验", "用户资料管理"],
            "order": ["创建订单", "订单状态机", "履约跟踪"],
            "payment": ["支付发起", "退款处理", "结算对账"],
            "notification": ["模板管理", "多渠道投递", "投递状态追踪"],
            "catalog": ["商品维护", "库存同步", "类目检索"],
            "risk": ["规则评估", "风险拦截", "人工审核"],
            "reporting": ["指标计算", "报表导出", "趋势分析"],
        }
        return labels.get(domain, matches[:3] or ["业务处理"])

    def _entities_for(self, domain: str) -> list[str]:
        return {
            "user": ["User", "Account", "Role"],
            "order": ["Order", "OrderItem", "OrderStatus"],
            "payment": ["Payment", "Refund", "Settlement"],
            "notification": ["Message", "Template", "DeliveryLog"],
            "catalog": ["Product", "Sku", "Inventory"],
            "risk": ["RiskRule", "RiskDecision", "ReviewTask"],
            "reporting": ["Metric", "Report", "Dashboard"],
        }.get(domain, ["Aggregate", "Event"])
