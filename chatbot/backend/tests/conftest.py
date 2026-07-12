from __future__ import annotations

from pathlib import Path

import pytest

from porto_chatbot.settings import Settings


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
