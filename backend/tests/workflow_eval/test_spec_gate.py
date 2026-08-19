"""spec 基线门禁——``scripts/spec_baseline_eval.py`` 的 pytest 薄封装。

默认 report-only（产出报告不拦截）；``SPEC_EVAL_REPORT_ONLY=0`` 启用硬门禁
（loop 独立 judge 均分须超过模板基线）。无 LLM key 时 skip（环境问题非回归）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "spec_baseline_eval.py"
_REPORT_DIR = Path(__file__).resolve().parent / "reports"


def test_spec_baseline_gate():
    # 先按脚本同款方式加载 .env/.env.test 到 os.environ（pytest conftest 会隔离
    # LANGCHAIN_* 并禁用 env_file，直接 Settings() 会误判「无 LLM」而永久 skip）
    import importlib.util

    spec = importlib.util.spec_from_file_location("spec_baseline_eval", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._load_env()

    from porto_chatbot.settings import Settings

    if not Settings().agent_api_key:
        pytest.skip("无 LLM 配置 —— 跳过 spec 基线门禁")

    sys.argv = [str(_SCRIPT), "--max-prds", os.environ.get("SPEC_EVAL_MAX_PRDS", "1")]
    mod.main()  # 打印表格 + 写报告（runpy 会重复 exec，改调 main）

    reports = sorted(_REPORT_DIR.glob("spec_baseline_*.json"))
    assert reports, "脚本未产出报告"
    data = json.loads(reports[-1].read_text(encoding="utf-8"))
    baseline = data["overall"]["baseline_avg"]
    loop = data["overall"]["loop_avg"]
    if os.environ.get("SPEC_EVAL_REPORT_ONLY", "1") != "1":
        assert loop > baseline, (
            f"spec loop（独立 judge {loop}）未超过模板基线（{baseline}）——"
            "evaluator-optimizer 循环失效或发生分数通胀"
        )
