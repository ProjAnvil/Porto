"""RAG eval pytest 门禁 conftest——精简为 ``run_experiment()`` 的薄封装。

共享 helper（env 读取、LLM 构建、预检）在 ``experiment.py`` 中定义，
本文件仅提供 pytest skip 语义。实际编排逻辑见 ``experiment.run_experiment()``。
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
