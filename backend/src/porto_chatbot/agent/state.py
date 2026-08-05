from __future__ import annotations

import operator
from enum import StrEnum
from typing import Annotated, Any, TypedDict

from ..models import AgentStep, SourceChunk, SpecResult, Subsystem


class BusinessDomain(StrEnum):
    """启发式子系统识别的业务领域分类。"""

    USER = "user"
    ORDER = "order"
    PAYMENT = "payment"
    NOTIFICATION = "notification"
    CATALOG = "catalog"
    RISK = "risk"
    REPORTING = "reporting"


def _dict_merge(left: dict, right: dict) -> dict:
    """dict-merge reducer:右覆盖左,保留左独有的 key。

    用于 specs / spec_results —— generate 节点写完整 dict,PATCH /specs 经
    graph.update_state 只改单个 key(merge),两者共用此 reducer。
    """
    return {**(left or {}), **(right or {})}


class PortoAgentState(TypedDict, total=False):
    workflow_id: str
    project_name: str
    prd_file_id: str
    sources: list[SourceChunk]
    understanding: str
    subsystems: list[Subsystem]
    specs: Annotated[dict[str, str], _dict_merge]
    spec_results: Annotated[dict[str, SpecResult], _dict_merge]
    evaluation: dict[str, Any]
    steps: Annotated[list[AgentStep], operator.add]
    top_k: int | None
    current_step: str
    rework_passes: int
    needs_rework: bool


DOMAIN_HINTS = {
    BusinessDomain.USER: ["用户", "账户", "认证", "登录", "权限", "profile", "account", "auth"],
    BusinessDomain.ORDER: ["订单", "下单", "履约", "交易", "order", "checkout"],
    BusinessDomain.PAYMENT: ["支付", "收款", "退款", "结算", "payment", "refund", "settlement"],
    BusinessDomain.NOTIFICATION: ["通知", "短信", "邮件", "站内信", "notification", "message"],
    BusinessDomain.CATALOG: ["商品", "库存", "目录", "sku", "catalog", "inventory"],
    BusinessDomain.RISK: ["风控", "风险", "反欺诈", "审核", "risk", "fraud"],
    BusinessDomain.REPORTING: ["报表", "统计", "分析", "dashboard", "report"],
}
