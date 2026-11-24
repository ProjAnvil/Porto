from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from porto_chatbot.settings import Settings

# F5: disable the Settings env_file in test mode to isolate the production backend/.env
# (which holds the real LANGCHAIN_API_KEY). pydantic-settings reads the env_file directly,
# so delenv(os.environ) cannot block the file read; once it is patched to None, test-mode
# Settings reads only from environ (managed by _load_env_test / _isolate_llm_env) + defaults.
Settings.model_config["env_file"] = None


def _load_env_test() -> None:
    """Load backend/.env.test into the environment (setdefault, existing variables win).

    The test suite reads .env.test rather than .env, keeping it isolated from the production
    config. By default .env.test has no LLM key → tests take the degraded path (deterministic,
    no real LLM calls); once a key is set in .env.test, tests that are not mocked call the
    real LLM (for baseline / integration testing).

    Precedence: existing os.environ > .env.test > Settings defaults; explicit fixture
    arguments win over all.
    """
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


# pytest unit tests must be deterministic: isolate the LLM / critic / workflow env so the
# real key and config in .env.test cannot send tests to the real LLM or change their
# behavior. The key in .env.test is only for the baseline script
# (scripts/spec_baseline_eval.py). Fields with a pydantic-settings validation_alias are
# read from env and override setattr, so this has to be delenv rather than setattr.
_ENV_KEYS_TO_ISOLATE = [
    "LANGCHAIN_AGENT_PROVIDER",
    "LANGCHAIN_API_KEY",
    "LANGCHAIN_BASE_URL",
    "LANGCHAIN_MODEL",
    "LANGCHAIN_TEMPERATURE",
    "LANGCHAIN_MAX_TOKENS",
    "PORTO_CHATBOT_CRITIC_PROVIDER",
    "PORTO_CHATBOT_CRITIC_MODEL",
    "PORTO_CHATBOT_CRITIC_API_KEY",
    "PORTO_CHATBOT_CRITIC_BASE_URL",
    "PORTO_CHATBOT_SPEC_REFINE_ENABLED",
    "PORTO_CHATBOT_SPEC_REFINE_MAX_ITER",
    "PORTO_CHATBOT_SPEC_REFINE_CONCURRENCY",
    "PORTO_CHATBOT_SPEC_REFINE_PASS_SCORE",
    "PORTO_CHATBOT_WORKFLOW_REWORK_ENABLED",
    "PORTO_CHATBOT_WORKFLOW_REWORK_MAX_PASSES",
    "PORTO_CHATBOT_AGENT_STREAM_ENABLED",
    "PORTO_CHATBOT_AGENT_REQUEST_TIMEOUT",
]


@pytest.fixture(autouse=True)
def _isolate_llm_env(monkeypatch):
    """Isolate the LLM / critic / workflow env for every test so the real key in .env.test never
    leaks into unit tests.

    Fields with a pydantic-settings validation_alias are read from env and override setattr,
    so delenv is required; autouse makes sure tests that do not use sample_settings are
    isolated too.
    """
    for key in _ENV_KEYS_TO_ISOLATE:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _isolate_kb_dirs(monkeypatch, tmp_path):
    """Point kb_dirs at a temp dir so tests never touch the real ~/.scv.

    graph_enabled defaults to True while kb_dirs defaults to the real ~/.scv/analysis;
    on a machine that actually has structure.vg files, tests would load tens of MB
    (slow, non-deterministic). Fixtures passing kb_dirs= explicitly win over env.

    The directory is deliberately `_isolated_kb_root`, not `kb`: tests conventionally
    create their own tmp_path / "kb", and the same name trips FileExistsError.
    """
    kb = tmp_path / "_isolated_kb_root"
    kb.mkdir(exist_ok=True)
    monkeypatch.setenv("PORTO_CHATBOT_KB_DIRS", json.dumps([str(kb)]))


@pytest.fixture(autouse=True)
def _reset_rag_singletons():
    """Reset the IndexSupervisor / HealthMonitor singletons around each test so data_dir stays
    isolated."""
    from porto_chatbot.api.deps import reset_rag_singletons
    from porto_chatbot.vector_stores import VectorRepositoryFactory

    reset_rag_singletons()
    VectorRepositoryFactory.clear()
    yield
    reset_rag_singletons()
    VectorRepositoryFactory.clear()


@pytest.fixture(autouse=True)
def _reset_logging_state():
    """Reset logging configuration before each test for isolation."""
    from porto_chatbot.logging_utils import reset_logging

    reset_logging()
    yield
    reset_logging()


@pytest.fixture()
def sample_settings(tmp_path: Path) -> Settings:
    kb = tmp_path / "kb"
    kb.mkdir(exist_ok=True)
    (kb / "payment-platform.md").write_text(
        """
# Payment Platform

payment-service handles payment authorization, refund, settlement, and channel routing.
risk-service evaluates fraud rules before high value transactions.
notification-service sends payment result messages.

支付平台包含支付、退款、结算、渠道路由、风控审核和支付结果通知能力。
""",
        encoding="utf-8",
    )
    return Settings(
        kb_dirs=[kb],
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        embedding_dimensions=128,
        embedding_provider="local",
    )


@pytest.fixture()
def sample_prd() -> str:
    return """
# 互联网支付交易平台

目标：支持用户下单后完成支付、退款、结算，并对高风险交易进行风控审核。
系统需要通知商户和用户支付结果，支持订单状态追踪、支付渠道路由和对账报表。
"""
