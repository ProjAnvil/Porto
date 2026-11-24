"""Conftest for the RAG eval pytest gates: a thin wrapper around ``run_experiment()``.

The shared helpers (env reading, LLM construction, precheck) are defined in
``experiment.py``; this file only provides the pytest skip semantics:

- ``gate_env``: full gate (one precheck of the generation LLM + the judge LLM).
- ``llm_env``: generation LLM only (rule-verdict gates such as refusal need no judge).
"""
from __future__ import annotations

import pytest

from .experiment import precheck, read_env_test


@pytest.fixture(scope="session")
def gate_env():
    """Gate precondition: the key is present and the LLM / judge really work (one precheck call).

    Anything unreachable is skipped (an environment issue, not a regression). Returns the env
    dict for the test to pass straight to run_experiment.
    """
    env = read_env_test()
    if not env.get("LANGCHAIN_API_KEY"):
        pytest.skip("无 LANGCHAIN_API_KEY（.env.test）—— 跳过 RAG 集成门禁")
    try:
        precheck(env)
    except Exception:
        pytest.skip("LLM/judge 预检失败 —— 跳过门禁（环境问题，非回归）")
    return env


@pytest.fixture(scope="session")
def llm_env():
    """Gate precondition with the generation LLM only (for refusal / multi-turn generation; a
    missing judge does not matter)."""
    env = read_env_test()
    if not env.get("LANGCHAIN_API_KEY"):
        pytest.skip("无 LANGCHAIN_API_KEY（.env.test）—— 跳过门禁")
    try:
        precheck(env, need_judge=False, need_generation=True)
    except Exception:
        pytest.skip("LLM 预检失败 —— 跳过门禁（环境问题，非回归）")
    return env
