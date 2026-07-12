"""纯启发式辅助：降级路径用的关键词匹配、文本抽取、子系统 schema 与归一化。

这些函数不依赖 LLM，是 understand/identify 节点降级路径与 LLM 输出校验的基础。
"""
from __future__ import annotations

import re
from datetime import UTC, datetime

from .state import DOMAIN_HINTS


def infer_project_name(text: str) -> str:
    first = next((line.strip("# ：:") for line in text.splitlines() if line.strip()), "")
    return first[:40] or f"Porto 项目 {datetime.now(UTC).strftime('%Y%m%d%H%M')}"


def summary_sentence(text: str) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    return clean[:180] + ("..." if len(clean) > 180 else "")


def extract_bullets(text: str, keywords: list[str]) -> list[str]:
    lines = [re.sub(r"^[-*#\d.、\s]+", "", line).strip() for line in text.splitlines()]
    matches = [line for line in lines if line and any(k.lower() in line.lower() for k in keywords)]
    return matches or [summary_sentence(text)]


def extract_entities(text: str) -> list[str]:
    candidates = re.findall(r"[一-鿿A-Za-z]{2,}(?:用户|订单|支付|账户|商品|通知|规则|任务|报表|服务|系统|记录)", text)
    seen: list[str] = []
    for item in candidates:
        if item not in seen:
            seen.append(item)
    return seen or ["用户", "业务流程", "需求记录"]


def matched_domains(text: str) -> dict[str, list[str]]:
    lower = text.lower()
    result: dict[str, list[str]] = {}
    for domain, hints in DOMAIN_HINTS.items():
        found = [h for h in hints if h.lower() in lower]
        if found:
            result[domain] = found
    return result


def responsibility_for(domain: str) -> str:
    return {
        "user": "负责用户身份、账户资料和权限边界",
        "order": "负责订单生命周期、交易状态和履约协同",
        "payment": "负责支付、退款、结算和资金通道集成",
        "notification": "负责消息模板、通知投递和触达记录",
        "catalog": "负责商品目录、库存快照和 SKU 信息",
        "risk": "负责风险识别、规则决策和审核流转",
        "reporting": "负责指标聚合、报表查询和运营分析",
    }.get(domain, "负责核心业务能力")


def capabilities_for(domain: str, matches: list[str]) -> list[str]:
    labels = {
        "user": ["注册登录", "权限校验", "用户资料管理"],
        "order": ["创建订单", "订单状态机", "履约跟踪"],
        "payment": ["支付发起", "退款处理", "结算对账"],
        "notification": ["模板管理", "多渠道投递", "投递状态追踪"],
        "catalog": ["商品维护", "库存同步", "类目检索"],
        "risk": ["规则评估", "风险拦截", "人工审核"],
        "reporting": ["指标计算", "报表导出", "趋势分析"],
    }
    return labels.get(domain, matches[:3] or ["业务处理"])


def entities_for(domain: str) -> list[str]:
    return {
        "user": ["User", "Account", "Role"],
        "order": ["Order", "OrderItem", "OrderStatus"],
        "payment": ["Payment", "Refund", "Settlement"],
        "notification": ["Message", "Template", "DeliveryLog"],
        "catalog": ["Product", "Sku", "Inventory"],
        "risk": ["RiskRule", "RiskDecision", "ReviewTask"],
        "reporting": ["Metric", "Report", "Dashboard"],
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
                        "type": {"type": "string", "enum": ["new", "extend", "existing"]},
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
        "type": raw_type if raw_type in ("new", "extend", "existing") else "new",
        "responsibility": str(d.get("responsibility", "")).strip() or "（LLM 未给出职责）",
        "capabilities": [str(c) for c in (d.get("capabilities") or [])][:12],
        "data_entities": [str(e) for e in (d.get("data_entities") or [])][:12],
        "dependencies": [str(dep) for dep in (d.get("dependencies") or [])][:12],
    }
