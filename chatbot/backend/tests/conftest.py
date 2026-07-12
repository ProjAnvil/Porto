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


@pytest.fixture()
def sample_settings(tmp_path: Path) -> Settings:
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
