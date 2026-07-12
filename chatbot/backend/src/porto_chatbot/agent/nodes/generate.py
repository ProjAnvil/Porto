from __future__ import annotations

from ...models import SpecResult, Subsystem
from ...specs import SpecContext, generate_spec_with_loop
from ..state import PortoAgentState


def generate_specs(agent, state: PortoAgentState) -> PortoAgentState:
    agent.logger.info(
        "step generate_specs start workflow_id=%s subsystems=%s",
        state["workflow_id"],
        len(state["subsystems"]),
    )
    subs = state["subsystems"]

    def _gen(sub: Subsystem) -> SpecResult:
        # 浅拷贝 state：并行时各子任务 tool 检索写各自的 tool_sources，互不干扰
        sub_ctx = SpecContext(
            llm=agent.llm,
            state={**state},
            settings=agent.settings,
            vector_store=agent.vector_store,
        )
        return generate_spec_with_loop(sub_ctx, sub)

    results: dict[str, SpecResult] = {}
    parallel = (
        agent.settings.spec_refine_parallel
        and agent.llm.enabled
        and agent.settings.spec_refine_enabled
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
    agent.logger.info(
        "step generate_specs finish specs=%s used_llm=%s iterations=%s",
        len(specs),
        used_llm,
        total_iters,
    )
    return agent._with_step(
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
