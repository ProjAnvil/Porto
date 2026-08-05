"""Spec evaluator-optimizer 子图：把 loop.py 的四重终止迭代封装为 LangGraph 子图。

单个子系统的 ``generate → critique → refine → …`` 循环编码为可被父图 Send fan-out
复用的子图：

    START → init_spec → critique → [should_stop] → { finalize → END | refine → critique }

子图状态 ``SpecSubgraphState`` **必须**声明
``spec_results: Annotated[dict, _dict_merge]``（审计 B1）：父图将各子图实例的返回
``spec_results`` 经 reducer 合并，确保多子系统的产物不会被 LangGraph 静默丢弃。

行为等价 ``specs.loop.generate_spec_with_loop``；该模块是 ``Send`` fan-out 友好的
LangGraph 子图版本，原函数仍保留以向后兼容。
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ..agent.state import _dict_merge
from ..models import SpecAttempt, SpecResult, Subsystem
from ..models.enums import SpecVerdict
from .context import SpecContext
from .steps import critique_spec, generate_initial_spec, refine_spec
from .template import render_template_spec

#: 与 ``loop.py`` 同步：反馈摘要截断长度。
_FEEDBACK_DIGEST_MAX = 200


class SpecSubgraphState(TypedDict, total=False):
    """Spec 子图状态。

    输入：``sub`` + ``ctx_*`` 字段（SpecContext 展开，避免 dataclass 直接序列化）。
    输出：``spec_results[sub.name] = SpecResult(...)``。

    ``spec_results`` 必须声明 ``Annotated[dict, _dict_merge]`` —— 父图多个子系统
    并行 Send 时，各子图实例的返回值通过 reducer 合并（B1 阻断项修正），否则产物
    会被 LangGraph 静默丢弃。``_dict_merge`` 复用自 ``agent.state``，与父图同款 reducer。
    """

    # ── 输入 ──
    sub: Any  # Subsystem
    prd_file_id: str
    # ── 迭代状态 ──
    current_spec: str
    best_spec: str
    best_score: int
    used_chars: int
    attempts: list[SpecAttempt]
    iteration: int
    feedback: str
    tool_meta: dict
    truncated: bool          # init_spec 检测到工具截断 → 直接 finalize
    best_improved: bool      # 本次 critique 是否提升 best（③ 退化检测）
    critic_failed: bool      # 本次 critique_spec 返回 None（critic 不可用）
    # ── SpecContext 展开 ──
    ctx_backend: Any
    ctx_llm: Any
    ctx_state: dict
    ctx_settings: Any
    ctx_vector_store: Any
    ctx_critic_llm: Any
    ctx_sema: Any             # M3: threading.Semaphore,init_spec 入口限流
    # ── 输出（B1：必须用 reducer）──
    spec_results: Annotated[dict, _dict_merge]
    specs: Annotated[dict, _dict_merge]  # {sub.name: final_text},供 workflow_store / PATCH /specs
    current_step: str                     # 固定 "generate",供 _sync_status 投影


# ----------------------------- helpers ----------------------------- #


def _ctx(state: SpecSubgraphState) -> SpecContext:
    """从 ``ctx_*`` 字段重建 SpecContext。

    每次调用都重建：SpecContext 含 backend/llm 等运行时引用，state 直接存会触发
    LangGraph 序列化；拆为字段入 state、用时再拼装。``SpecContext.__post_init__``
    在 ``backend=None`` 且 ``llm!=None`` 时自动包一层 LangchainBackend（向后兼容）。
    """
    return SpecContext(
        backend=state.get("ctx_backend"),
        llm=state.get("ctx_llm"),
        state=state.get("ctx_state") or {},
        settings=state.get("ctx_settings"),
        vector_store=state.get("ctx_vector_store"),
        critic_llm=state.get("ctx_critic_llm"),
    )


def _llm_active(ctx: SpecContext) -> bool:
    """LLM + spec_refine 双开关是否启用（与 loop.py 同一守卫）。"""
    return bool(
        ctx.llm
        and getattr(ctx.llm, "enabled", False)
        and ctx.settings
        and getattr(ctx.settings, "spec_refine_enabled", False)
    )


def _budget_chars(settings: Any) -> int:
    """token 预算粗略折算字符数（4 字符 ≈ 1 token，与 loop.py 一致）。"""
    return getattr(settings, "spec_refine_budget_tokens", 40000) * 4


# ----------------------------- 节点 ----------------------------- #


def init_spec(state: SpecSubgraphState, *, config=None) -> dict:
    """生成首版 spec（入口经 ``ctx_sema`` 限流，M3）。

    - LLM 未启用 / spec_refine 关闭 → 模板降级（``used_llm=False`` 由 finalize 决定）
    - LLM 启用：``generate_initial_spec``，工具截断 → 标记 ``truncated=True`` 直接 finalize
    - LLM 生成空 → 模板兜底

    **运行时引用来源(Task 9)**:父图经 ``Send`` fan-out 时,Send payload 只含可序列化字段
    (会被 checkpoint),agent 的 backend/llm/settings/.../sema 经 ``config`` 传递。
    本节点从 ``config["configurable"]["agent"]`` 取出并填入子图 state。单元测试可直接在
    state 里放 ``ctx_*`` 字段(不传 config),向后兼容。

    M3 并发限流:``ctx_sema`` 是 agent 构造的 ``threading.Semaphore(spec_refine_concurrency)``。
    ``with`` 包住 init 主体(含 ``generate_initial_spec`` 的 LLM/工具调用,最耗时阶段),
    异常时自动释放。LangGraph 同步 fan-out 下为语义限流;async/pool 执行器下生效为真实信号量。
    """
    # 从 config 注入运行时引用(Send payload 序列化约束);无 config 时(state 已含 ctx_*)跳过
    agent = (config or {}).get("configurable", {}).get("agent")
    if agent is not None:
        state = {
            **state,
            "ctx_backend": agent.backend,
            "ctx_llm": agent.llm,
            "ctx_settings": agent.settings,
            "ctx_vector_store": agent.vector_store,
            "ctx_critic_llm": agent.critic_llm,
            "ctx_sema": agent._spec_sema,
        }
    sema = state.get("ctx_sema")
    if sema is not None:
        with sema:
            return _init_spec_impl(state)
    return _init_spec_impl(state)


def _init_spec_impl(state: SpecSubgraphState) -> dict:
    ctx = _ctx(state)
    sub: Subsystem = state["sub"]

    if not _llm_active(ctx):
        spec = render_template_spec(ctx, sub)
        return {
            "current_spec": spec,
            "best_spec": spec,
            "best_score": -1,
            "used_chars": len(spec),
            "attempts": [],
            "iteration": 0,
            "truncated": False,
            "tool_meta": {},
        }

    spec, tool_meta = generate_initial_spec(ctx, sub)
    if tool_meta.get("truncated"):
        # 工具截断 → 跳过 critique/refine，由 finalize 直接收尾
        return {
            "current_spec": spec,
            "best_spec": spec,
            "best_score": -1,
            "used_chars": len(spec),
            "attempts": [],
            "iteration": 0,
            "truncated": True,
            "tool_meta": tool_meta,
        }

    if not spec:
        spec = render_template_spec(ctx, sub)  # 生成失败 → 模板兜底

    return {
        "current_spec": spec,
        "best_spec": spec,
        "best_score": -1,
        "used_chars": len(spec),
        "attempts": [],
        "iteration": 0,
        "truncated": False,
        "tool_meta": tool_meta,
    }


def critique(state: SpecSubgraphState) -> dict:
    """评判 ``current_spec``，更新 ``attempts`` / ``best_*`` / ``feedback`` / ``iteration``。

    工具截断 / LLM 未启用 → 空返回（_should_stop 会直接路由 finalize）。
    """
    # 工具截断 → 交给 finalize，不再 critique
    if state.get("truncated"):
        return {}
    ctx = _ctx(state)
    if not _llm_active(ctx):
        return {}

    sub: Subsystem = state["sub"]
    iteration = state.get("iteration", 0) + 1
    spec = state["current_spec"]
    best_spec = state.get("best_spec") or spec
    best_score = state.get("best_score", -1)
    attempts = list(state.get("attempts") or [])
    used_chars = state.get("used_chars", len(spec))

    critique_obj = critique_spec(ctx, sub, spec)
    if critique_obj is None:
        # critic 不可用 → 接受当前 spec（与 loop.py 一致）
        attempts.append(
            SpecAttempt(
                version=iteration,
                verdict=SpecVerdict.NEEDS_IMPROVEMENT,
                feedback_digest="critic 不可用",
            )
        )
        return {
            "iteration": iteration,
            "attempts": attempts,
            "feedback": "",
            "best_improved": False,
            "critic_failed": True,
        }

    attempts.append(
        SpecAttempt(
            version=iteration,
            score=critique_obj.score,
            verdict=critique_obj.verdict,
            feedback_digest=critique_obj.feedback[:_FEEDBACK_DIGEST_MAX],
        )
    )
    used_chars += len(critique_obj.feedback)

    improved = critique_obj.score > best_score
    if improved:
        best_spec = spec
        best_score = critique_obj.score

    return {
        "iteration": iteration,
        "attempts": attempts,
        "feedback": critique_obj.feedback,
        "best_spec": best_spec,
        "best_score": best_score,
        "used_chars": used_chars,
        "best_improved": improved,
        "critic_failed": False,
    }


def _should_stop(state: SpecSubgraphState) -> str:
    """四重终止条件路由。

    等价 ``loop.py`` 循环内的 break 条件（任一命中 → ``finalize``，否则 ``refine``）：

    ① ``verdict==PASS`` 或 ``score>=pass_score``（达标）
    ② ``iteration>=max_iter``（达到迭代上限）
    ③ 本次 score 未超越历史 best（震荡/退化；持平也算退化，与 loop.py 的 ``<=`` 一致）
    ④ ``used_chars>budget*4``（预算上限）

    工具截断 / LLM 未启用 / critic 不可用 → 也走 ``finalize``。
    """
    # 工具截断 → 直接 finalize
    if state.get("truncated"):
        return "finalize"

    ctx = _ctx(state)
    if not _llm_active(ctx):
        return "finalize"

    settings = ctx.settings
    iteration = state.get("iteration", 0)
    attempts = state.get("attempts") or []
    used_chars = state.get("used_chars", 0)

    last = attempts[-1] if attempts else None

    # critic 不可用 → 接受当前（在 ①-④ 之前，对应 loop.py 的 critique is None 分支）
    if state.get("critic_failed"):
        return "finalize"

    # ① 达标
    if last is not None and (
        last.verdict == SpecVerdict.PASS
        or last.score >= settings.spec_refine_pass_score
    ):
        return "finalize"
    # ② max_iter
    if iteration >= settings.spec_refine_max_iter:
        return "finalize"
    # ③ 退化：本次未超越 best（best_improved=False，含持平情况）
    if not state.get("best_improved", True):
        return "finalize"
    # ④ 预算上限
    if used_chars > _budget_chars(settings):
        return "finalize"
    return "refine"


def refine(state: SpecSubgraphState) -> dict:
    """根据 ``feedback`` 修订 ``current_spec``，累加 ``used_chars``。"""
    ctx = _ctx(state)
    sub: Subsystem = state["sub"]
    spec = state["current_spec"]
    feedback = state.get("feedback", "")
    refined = refine_spec(ctx, sub, spec, feedback)
    if refined and refined.strip():
        spec = refined
    used_chars = state.get("used_chars", 0) + len(spec)
    return {"current_spec": spec, "used_chars": used_chars}


def _emit(sub_name: str, spec_result: SpecResult) -> dict:
    """统一 finalize 输出:``spec_results`` + ``specs``(派生)+ ``current_step``。

    - ``spec_results``:``{sub_name: SpecResult}``,经 ``_dict_merge`` 合并各子图实例(B1)。
    - ``specs``:``{sub_name: final_text}``,供 workflow_store 持久化 + PATCH /specs + 前端。
    - ``current_step``:固定 ``"generate"``,供 ``_sync_status`` 投影到 workflows.current_step。
    """
    return {
        "spec_results": {sub_name: spec_result},
        "specs": {sub_name: spec_result.final},
        "current_step": "generate",
    }


def finalize(state: SpecSubgraphState) -> dict:
    """选 ``best_spec``，输出 ``spec_results[sub.name] = SpecResult(...)``。

    ``truncated`` 字段语义与 ``loop.py`` 一致：仅 max_iter / budget 命中为 ``True``；
    工具截断（``tool_meta["truncated"]``）、达标、退化、critic 不可用均为 ``False``。
    """
    ctx = _ctx(state)
    settings = ctx.settings
    sub: Subsystem = state["sub"]
    llm_active = _llm_active(ctx)

    # ── 工具截断：attempts=[], iterations=0, used_llm=True ──
    if state.get("truncated"):
        spec_result = SpecResult(
            final=state.get("current_spec", ""),
            attempts=[],
            iterations=0,
            truncated=False,
            used_llm=True,
            tool_meta=state.get("tool_meta", {}),
        )
        return _emit(sub.name, spec_result)

    # ── LLM 未启用 → 模板降级，used_llm=False ──
    if not llm_active:
        spec_result = SpecResult(
            final=state.get("current_spec", ""),
            attempts=[],
            iterations=0,
            truncated=False,
            used_llm=False,
            tool_meta={},
        )
        return _emit(sub.name, spec_result)

    best_spec = state.get("best_spec") or state.get("current_spec", "")
    attempts = state.get("attempts") or []
    iteration = state.get("iteration", 0)
    used_chars = state.get("used_chars", 0)
    last = attempts[-1] if attempts else None

    # truncated 推断：按 _should_stop 的优先级反推
    # 仅 ④ 预算 / ② max_iter 命中 → True；① 达标 / ③ 退化 / critic 不可用 → False
    truncated = False
    if last is not None and not state.get("critic_failed"):
        is_pass = (
            last.verdict == SpecVerdict.PASS
            or last.score >= settings.spec_refine_pass_score
        )
        is_degradation = not state.get("best_improved", True)
        if is_pass or is_degradation:
            truncated = False
        elif used_chars > _budget_chars(settings):
            truncated = True
        elif iteration >= settings.spec_refine_max_iter:
            truncated = True

    spec_result = SpecResult(
        final=best_spec,
        attempts=attempts,
        iterations=len(attempts),
        truncated=truncated,
        used_llm=True,
        tool_meta=state.get("tool_meta", {}),
    )
    return _emit(sub.name, spec_result)


# ----------------------------- 子图构建 ----------------------------- #


def build_spec_subgraph():
    """编译 spec evaluator-optimizer 子图。

    返回 ``CompiledGraph``，可直接 ``add_node`` 进父图；父图通过 ``Send`` fan-out
    对每个子系统启动一个子图实例，各实例的 ``spec_results`` 经 ``_dict_merge`` 合并。
    """
    g = StateGraph(SpecSubgraphState)
    g.add_node("init_spec", init_spec)
    g.add_node("critique", critique)
    g.add_node("refine", refine)
    g.add_node("finalize", finalize)
    g.add_edge(START, "init_spec")
    g.add_edge("init_spec", "critique")
    g.add_conditional_edges(
        "critique",
        _should_stop,
        {"finalize": "finalize", "refine": "refine"},
    )
    g.add_edge("refine", "critique")
    g.add_edge("finalize", END)
    return g.compile()
