from __future__ import annotations

from typing import Any, TypedDict

from ..models import AgentStep, SourceChunk, SpecResult, Subsystem


class PortoAgentState(TypedDict, total=False):
    workflow_id: str
    project_name: str
    prd_text: str
    sources: list[SourceChunk]
    understanding: str
    subsystems: list[Subsystem]
    specs: dict[str, str]
    spec_results: dict[str, SpecResult]
    evaluation: dict[str, Any]
    steps: list[AgentStep]
    top_k: int | None
    rework_passes: int
    needs_rework: bool


DOMAIN_HINTS = {
    "user": ["用户", "账户", "认证", "登录", "权限", "profile", "account", "auth"],
    "order": ["订单", "下单", "履约", "交易", "order", "checkout"],
    "payment": ["支付", "收款", "退款", "结算", "payment", "refund", "settlement"],
    "notification": ["通知", "短信", "邮件", "站内信", "notification", "message"],
    "catalog": ["商品", "库存", "目录", "sku", "catalog", "inventory"],
    "risk": ["风控", "风险", "反欺诈", "审核", "risk", "fraud"],
    "reporting": ["报表", "统计", "分析", "dashboard", "report"],
}
