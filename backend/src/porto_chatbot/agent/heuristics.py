"""纯启发式辅助：降级路径用的关键词匹配、文本抽取、子系统 schema 与归一化。

这些函数不依赖 LLM，是 understand/identify 节点降级路径与 LLM 输出校验的基础。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from ..models.enums import SubsystemType
from .state import DOMAIN_HINTS, BusinessDomain

_MAX_PROJECT_NAME_CHARS = 40
_SUMMARY_MAX_CHARS = 180
_MAX_LIST_ITEMS = 12


def infer_project_name(text: str) -> str:
    first = next((line.strip("# ：:") for line in text.splitlines() if line.strip()), "")
    return first[:_MAX_PROJECT_NAME_CHARS] or f"Porto 项目 {datetime.now(UTC).strftime('%Y%m%d%H%M')}"


def summary_sentence(text: str) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    return clean[:_SUMMARY_MAX_CHARS] + ("..." if len(clean) > _SUMMARY_MAX_CHARS else "")


def extract_bullets(text: str, keywords: list[str]) -> list[str]:
    lines = [re.sub(r"^[-*#\d.、\s]+", "", line).strip() for line in text.splitlines()]
    matches = [line for line in lines if line and any(k.lower() in line.lower() for k in keywords)]
    return matches or [summary_sentence(text)]


def extract_entities(text: str) -> list[str]:
    candidates = re.findall(
        r"[一-鿿A-Za-z]{2,}(?:用户|订单|支付|账户|商品|通知|规则|任务|报表|服务|系统|记录)", text
    )
    seen: list[str] = []
    for item in candidates:
        if item not in seen:
            seen.append(item)
    return seen or ["用户", "业务流程", "需求记录"]


def matched_domains(text: str) -> dict[BusinessDomain, list[str]]:
    lower = text.lower()
    result: dict[BusinessDomain, list[str]] = {}
    for domain, hints in DOMAIN_HINTS.items():
        found = [h for h in hints if h.lower() in lower]
        if found:
            result[domain] = found
    return result


def responsibility_for(domain: BusinessDomain) -> str:
    return {
        BusinessDomain.USER: "负责用户身份、账户资料和权限边界",
        BusinessDomain.ORDER: "负责订单生命周期、交易状态和履约协同",
        BusinessDomain.PAYMENT: "负责支付、退款、结算和资金通道集成",
        BusinessDomain.NOTIFICATION: "负责消息模板、通知投递和触达记录",
        BusinessDomain.CATALOG: "负责商品目录、库存快照和 SKU 信息",
        BusinessDomain.RISK: "负责风险识别、规则决策和审核流转",
        BusinessDomain.REPORTING: "负责指标聚合、报表查询和运营分析",
    }.get(domain, "负责核心业务能力")


def capabilities_for(domain: BusinessDomain, matches: list[str]) -> list[str]:
    labels = {
        BusinessDomain.USER: ["注册登录", "权限校验", "用户资料管理"],
        BusinessDomain.ORDER: ["创建订单", "订单状态机", "履约跟踪"],
        BusinessDomain.PAYMENT: ["支付发起", "退款处理", "结算对账"],
        BusinessDomain.NOTIFICATION: ["模板管理", "多渠道投递", "投递状态追踪"],
        BusinessDomain.CATALOG: ["商品维护", "库存同步", "类目检索"],
        BusinessDomain.RISK: ["规则评估", "风险拦截", "人工审核"],
        BusinessDomain.REPORTING: ["指标计算", "报表导出", "趋势分析"],
    }
    return labels.get(domain, matches[:3] or ["业务处理"])


def entities_for(domain: BusinessDomain) -> list[str]:
    return {
        BusinessDomain.USER: ["User", "Account", "Role"],
        BusinessDomain.ORDER: ["Order", "OrderItem", "OrderStatus"],
        BusinessDomain.PAYMENT: ["Payment", "Refund", "Settlement"],
        BusinessDomain.NOTIFICATION: ["Message", "Template", "DeliveryLog"],
        BusinessDomain.CATALOG: ["Product", "Sku", "Inventory"],
        BusinessDomain.RISK: ["RiskRule", "RiskDecision", "ReviewTask"],
        BusinessDomain.REPORTING: ["Metric", "Report", "Dashboard"],
    }.get(domain, ["Aggregate", "Event"])


def subsystem_schema() -> dict:
    """identify_subsystems 节点约束 LLM 结构化输出的 JSON schema。"""
    return {
        "type": "object",
        "properties": {
            "subsystems": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "子系统名称，形如 xxx-service"},
                        "type": {"type": "string", "enum": [e.value for e in SubsystemType]},
                        "responsibility": {"type": "string"},
                        "capabilities": {"type": "array", "items": {"type": "string"}},
                        "data_entities": {"type": "array", "items": {"type": "string"}},
                        "dependencies": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["name", "responsibility"],
                },
            }
        },
        "required": ["subsystems"],
    }


def normalize_sub_dict(d: object) -> dict | None:
    """把 LLM 输出的子系统 dict 安全归一化为 Subsystem 构造参数。"""
    if not isinstance(d, dict):
        return None
    name = str(d.get("name", "")).strip()
    if not name:
        return None
    raw_type = d.get("type", "new")
    return {
        "name": name,
        "type": raw_type if raw_type in [e.value for e in SubsystemType] else "new",
        "responsibility": str(d.get("responsibility", "")).strip() or "（LLM 未给出职责）",
        "capabilities": [str(c) for c in (d.get("capabilities") or [])][:_MAX_LIST_ITEMS],
        "data_entities": [str(e) for e in (d.get("data_entities") or [])][:_MAX_LIST_ITEMS],
        "dependencies": [str(dep) for dep in (d.get("dependencies") or [])][:_MAX_LIST_ITEMS],
    }
