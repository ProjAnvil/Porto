"""spec baseline gate: a thin pytest wrapper around ``scripts/spec_baseline_eval.py``.

Report-only by default (produces the report without blocking);
``SPEC_EVAL_REPORT_ONLY=0`` turns on the hard gate (the loop's mean independent-judge
score must beat the template baseline). Skips without an LLM key (an environment
issue, not a regression).
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
# Same families tests/conftest.py::_isolate_llm_env deletes for the duration of every test.
_LLM_ENV_PREFIXES = ("LANGCHAIN_", "PORTO_CHATBOT_CRITIC_")


@pytest.fixture(scope="session")
def process_llm_env() -> dict[str, str]:
    """LLM config from the process env, snapshotted before the per-test isolation runs."""
    return {k: v for k, v in os.environ.items() if k.startswith(_LLM_ENV_PREFIXES)}


def test_spec_baseline_gate(monkeypatch, process_llm_env):
    # CI injects secrets through the environment (there is no .env file), but
    # tests/conftest.py::_isolate_llm_env delenv's LANGCHAIN_* inside every test. Restore
    # them here, otherwise Settings() never sees the key and this gate skips forever.
    for key, value in process_llm_env.items():
        monkeypatch.setenv(key, value)
    # Then load .env/.env.test the way the script does (conftest disabled the Settings
    # env_file, so a bare Settings() would wrongly report "no LLM" and skip for good).
    import importlib.util

    spec = importlib.util.spec_from_file_location("spec_baseline_eval", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._load_env()

    from porto_chatbot.settings import Settings

    if not Settings().agent_api_key:
        pytest.skip("无 LLM 配置 —— 跳过 spec 基线门禁")

    sys.argv = [str(_SCRIPT), "--max-prds", os.environ.get("SPEC_EVAL_MAX_PRDS", "1")]
    mod.main()  # prints the table + writes the report (runpy would exec twice, so call main() directly)

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
