from __future__ import annotations

import os
from pathlib import Path

import pytest

from porto_chatbot.settings import Settings


def _load_env_test() -> None:
    """加载 backend/.env.test 到环境变量（setdefault，不覆盖已有环境变量）。

    测试套件读 .env.test 而非 .env，与生产配置隔离。默认 .env.test 不配 LLM
    key → 测试走降级路径（确定性，不调真 LLM）；在 .env.test 配 key 后，未 mock
    的测试会走真 LLM（用于基线/集成测试）。

    优先级：已有 os.environ > .env.test > Settings 默认；fixture 显式参数最高。
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


# pytest 单元测试必须确定性：隔离 LLM/critic/工作流相关 env，避免 .env.test 的
# 真 key / 配置让测试走真 LLM 或改变行为。.env.test 的 key 仅供基线脚本
# (scripts/spec_baseline_eval.py) 使用。pydantic-settings 的 validation_alias
# 字段会从 env 读取并覆盖 setattr，所以必须 delenv 而非 setattr。
_ENV_KEYS_TO_ISOLATE = [
    "LANGCHAIN_AGENT_PROVIDER", "LANGCHAIN_API_KEY", "LANGCHAIN_BASE_URL",
    "LANGCHAIN_MODEL", "LANGCHAIN_TEMPERATURE", "LANGCHAIN_MAX_TOKENS",
    "PORTO_CHATBOT_CRITIC_PROVIDER", "PORTO_CHATBOT_CRITIC_MODEL",
    "PORTO_CHATBOT_CRITIC_API_KEY", "PORTO_CHATBOT_CRITIC_BASE_URL",
    "PORTO_CHATBOT_SPEC_REFINE_ENABLED", "PORTO_CHATBOT_SPEC_REFINE_MAX_ITER",
    "PORTO_CHATBOT_SPEC_REFINE_PARALLEL", "PORTO_CHATBOT_SPEC_REFINE_PASS_SCORE",
    "PORTO_CHATBOT_WORKFLOW_REWORK_ENABLED", "PORTO_CHATBOT_WORKFLOW_REWORK_MAX_PASSES",
    "PORTO_CHATBOT_AGENT_STREAM_ENABLED",
]


@pytest.fixture()
def sample_settings(tmp_path: Path, monkeypatch) -> Settings:
    for key in _ENV_KEYS_TO_ISOLATE:
        monkeypatch.delenv(key, raising=False)
    kb = tmp_path / "kb"
    kb.mkdir()
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
        kb_path=kb,
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
