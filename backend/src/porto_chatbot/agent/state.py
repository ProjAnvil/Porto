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


def _last_wins(left: Any, right: Any) -> Any:
    """last-wins reducer:右值非 None 则覆盖,否则保留左值。

    用于 ``current_step`` —— Send fan-out 时多个 spec 子图实例在同一 superstep
    并发写同一个值(``"generate"``);last_value channel 不接受同 step 多写入
    (InvalidUpdateError),reducer channel 可以。reducer 语义 = 最后写的赢,与
    last_value 等价(单写场景 right 覆盖 left,fan-out 场景多写同值)。
    """
    return right if right is not None else left


class PortoAgentState(TypedDict, total=False):
    workflow_id: str
    project_name: str
    prd_file_id: str
    sources: list[SourceChunk]
    understanding: str
    subsystems: list[Subsystem]
    specs: Annotated[dict[str, str], _dict_merge]
    spec_results: Annotated[dict[str, SpecResult], _dict_merge]
    steps: Annotated[list[AgentStep], operator.add]
    top_k: int | None
    current_step: Annotated[str, _last_wins]


DOMAIN_HINTS = {
    BusinessDomain.USER: ["用户", "账户", "认证", "登录", "权限", "profile", "account", "auth"],
    BusinessDomain.ORDER: ["订单", "下单", "履约", "交易", "order", "checkout"],
    BusinessDomain.PAYMENT: ["支付", "收款", "退款", "结算", "payment", "refund", "settlement"],
    BusinessDomain.NOTIFICATION: ["通知", "短信", "邮件", "站内信", "notification", "message"],
    BusinessDomain.CATALOG: ["商品", "库存", "目录", "sku", "catalog", "inventory"],
    BusinessDomain.RISK: ["风控", "风险", "反欺诈", "审核", "risk", "fraud"],
    BusinessDomain.REPORTING: ["报表", "统计", "分析", "dashboard", "report"],
}
