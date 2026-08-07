from __future__ import annotations

import os
from pathlib import Path

import pytest

from porto_chatbot.llm.client import LLMClient
from porto_chatbot.settings import Settings

from .loaders.domainrag import load_domainrag
from .provision import build_eval_kb

_ENV_TEST = Path(__file__).resolve().parents[2] / ".env.test"


def _read_env_test() -> dict[str, str]:
    """直读 backend/.env.test（绕过根 conftest 对 LANGCHAIN_* env 的 autouse 隔离）。"""
    out: dict[str, str] = {}
    if not _ENV_TEST.exists():
        return out
    for line in _ENV_TEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def _build_llm(env: dict[str, str]) -> LLMClient:
    settings = Settings(
        agent_provider=env.get("LANGCHAIN_AGENT_PROVIDER", "openai"),
        agent_api_key=env["LANGCHAIN_API_KEY"],
        agent_base_url=env.get("LANGCHAIN_BASE_URL") or None,
        agent_model=env.get("LANGCHAIN_MODEL", "gpt-4.1-mini"),
    )
    return LLMClient(settings)


@pytest.fixture(scope="session")
def _gate_ready():
    """门禁前置：key 在位 + LLM 实际可用（预检一次调用）。

    任何昂贵 fixture（eval_kb 建 1686 文档索引）都依赖本 fixture，避免在
    key 缺失/失效时白白构建索引。预检失败一律 skip（环境问题，非回归）。
    """
    env = _read_env_test()
    if not env.get("LANGCHAIN_API_KEY"):
        pytest.skip("无 LANGCHAIN_API_KEY（.env.test）—— 跳过 RAG 集成门禁")
    llm = _build_llm(env)
    if not llm.enabled:
        pytest.skip("LLM 未启用 —— 跳过 RAG 集成门禁")
    try:
        llm.complete("ping", user="reply: ok")
    except Exception:
        pytest.skip("生成 LLM key 无效或 endpoint 不可达 —— 跳过门禁（环境问题，非回归）")
    # 为 DeepEval（litellm 后端）配置 OpenAI-compatible judge env
    os.environ["OPENAI_API_KEY"] = env["LANGCHAIN_API_KEY"]
    if env.get("LANGCHAIN_BASE_URL"):
        os.environ["OPENAI_API_BASE"] = env["LANGCHAIN_BASE_URL"]
    # 预检 judge LLM（deepeval/litellm）：用一条微型 case 实测一次
    try:
        from deepeval.test_case import LLMTestCase

        from .metrics import build_metrics

        model = env.get("LANGCHAIN_MODEL", "gpt-4.1-mini")
        build_metrics(f"openai/{model}")["faithfulness"].measure(
            LLMTestCase(input="x", actual_output="y", retrieval_context=["z"])
        )
    except Exception:
        pytest.skip("judge LLM(deepeval/litellm) 无效或不可达 —— 跳过门禁（环境问题，非回归）")
    return env


@pytest.fixture(scope="session")
def domainrag_data(_gate_ready):
    """加载 DomainRAG；未下载时 pytest.skip。依赖 _gate_ready 避免无谓加载。"""
    try:
        return load_domainrag()
    except FileNotFoundError:
        pytest.skip("DomainRAG 未下载 —— 运行 make eval-dataset")


@pytest.fixture(scope="session")
def eval_kb(tmp_path_factory, domainrag_data):
    corpus, _ = domainrag_data
    tmp = tmp_path_factory.mktemp("eval_kb")
    return build_eval_kb(corpus, tmp)


@pytest.fixture(scope="session")
def eval_llm(_gate_ready):
    return _build_llm(_gate_ready)


@pytest.fixture(scope="session")
def judge_env(_gate_ready):
    return _gate_ready.get("LANGCHAIN_MODEL", "gpt-4.1-mini")
