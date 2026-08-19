"""RAG eval pytest 门禁 conftest——``run_experiment()`` 的薄封装。

共享 helper（env 读取、LLM 构建、预检）在 ``experiment.py`` 中定义，
本文件仅提供 pytest skip 语义：

- ``gate_env``：完整门禁（生成 LLM + judge LLM 预检一次）。
- ``llm_env``：仅生成 LLM（拒答等规则判定门禁，不需要 judge）。
"""
from __future__ import annotations

import pytest

from .experiment import precheck, read_env_test


@pytest.fixture(scope="session")
def gate_env():
    """门禁前置：key 在位 + LLM/judge 实际可用（预检一次调用）。

    不可达一律 skip（环境问题，非回归）。返回 env dict 供 test 直接传给 run_experiment。
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
    """仅生成 LLM 的门禁前置（拒答/多轮生成用，judge 缺失不影响）。"""
    env = read_env_test()
    if not env.get("LANGCHAIN_API_KEY"):
        pytest.skip("无 LANGCHAIN_API_KEY（.env.test）—— 跳过门禁")
    try:
        precheck(env, need_judge=False, need_generation=True)
    except Exception:
        pytest.skip("LLM 预检失败 —— 跳过门禁（环境问题，非回归）")
    return env
