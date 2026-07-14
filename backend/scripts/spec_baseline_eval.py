#!/usr/bin/env python
"""spec rubric 基线对照：模板生成（地板） vs LLM evaluator-optimizer loop。

验证 Phase 2 的核心命题——loop 是否真的比模板拼接产出更好的 spec。

跑法：
  cd backend
  cp .env.example .env          # 填入 LANGCHAIN_API_KEY 等
  uv run python scripts/spec_baseline_eval.py

输出：模板基线均分 vs LLM loop 均分（满分 12），以及提升幅度。
"""
from __future__ import annotations

import os
import statistics
from pathlib import Path

from porto_chatbot.agent import PortoAgent
from porto_chatbot.llm import LLMClient
from porto_chatbot.settings import Settings
from porto_chatbot.specs import SpecContext, critique_spec


def _load_env_test() -> None:
    """加载 backend/.env.test（若存在），与 conftest 一致，便于用测试配置跑基线。"""
    env_test = Path(__file__).resolve().parent.parent / ".env.test"
    if not env_test.exists():
        return
    for line in env_test.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_env_test()

SAMPLE_PRD = """
# 互联网支付交易平台

目标：支持用户下单后完成支付、退款、结算，并对高风险交易进行风控审核。
系统需要通知商户和用户支付结果，支持订单状态追踪、支付渠道路由和对账报表。
核心实体包括用户账户、订单、支付流水、风控规则和通知模板。
"""


def _check_llm() -> Settings:
    s = Settings()
    llm = LLMClient(s)
    if not llm.enabled:
        print("❌ 未检测到 LLM 配置。请在 backend/.env 配置：")
        print("   LANGCHAIN_AGENT_PROVIDER=openai   # 或 anthropic")
        print("   LANGCHAIN_API_KEY=sk-...")
        print("   LANGCHAIN_MODEL=gpt-4.1-mini")
        print("   LANGCHAIN_BASE_URL=               # 可选，代理地址")
        raise SystemExit(1)
    critic = "独立 critic" if s.critic_provider else "与 generator 同模型"
    print(f"✅ LLM 就绪：provider={s.agent_provider} model={s.agent_model} critic={critic}")
    return s


def _critique_template_specs(response, settings, critic_llm) -> list[int]:
    """模板路径 used_llm=False 无 attempts，用 critic 给模板 spec 打分作基线。"""
    ctx = SpecContext(
        llm=LLMClient(settings), state={}, settings=settings, critic_llm=critic_llm,
    )
    scores: list[int] = []
    for sub in response.subsystems:
        spec = response.specs.get(sub.name, "")
        critique = critique_spec(ctx, sub, spec)
        scores.append(critique.score if critique else 0)
        verdict = critique.verdict if critique else "N/A"
        print(f"   {sub.name}: score={critique.score if critique else 0} verdict={verdict}")
    return scores


def main() -> None:
    _check_llm()

    print("\n=== Run 1: 模板基线（spec_refine_enabled=False）===")
    s_off = Settings()
    s_off.spec_refine_enabled = False
    agent_off = PortoAgent(s_off)
    r_off = agent_off.run(SAMPLE_PRD, project_name="基线对照")
    print(f"识别 {len(r_off.subsystems)} 个子系统，模板 spec rubric 打分：")
    baseline = _critique_template_specs(r_off, s_off, agent_off.critic_llm)

    print("\n=== Run 2: LLM evaluator-optimizer loop（spec_refine_enabled=True）===")
    s_on = Settings()
    s_on.spec_refine_enabled = True
    agent_on = PortoAgent(s_on)
    r_on = agent_on.run(SAMPLE_PRD, project_name="基线对照")
    improved_avg = r_on.evaluation.get("spec_rubric_avg")
    iterations = r_on.steps[-1].data.get("iterations") if r_on.steps else None
    print(f"LLM loop 完成，总迭代次数：{iterations}，rubric 均分：{improved_avg}")

    b_avg = statistics.mean(baseline) if baseline else 0.0
    print("\n" + "=" * 50)
    print(f"模板基线均分 : {b_avg:.2f} / 12")
    print(f"LLM loop 均分 : {improved_avg} / 12")
    if improved_avg is not None:
        delta = improved_avg - b_avg
        sign = "✅ 提升" if delta > 0 else ("⚠️ 持平/下降" if delta <= 0 else "")
        print(f" delta       : {delta:+.2f}  {sign}")
    print("=" * 50)


if __name__ == "__main__":
    main()
