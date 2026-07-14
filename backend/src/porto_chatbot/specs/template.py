"""模板拼接的规格生成（LLM 不可用时的降级方案）。"""
from __future__ import annotations

from datetime import UTC, datetime

from ..models import Subsystem
from .context import SpecContext

# ----------------------------- 模板生成（fallback）----------------------------- #

def render_template_spec(ctx: SpecContext, sub: Subsystem) -> str:
    """模板拼接的规格（搬自原 agent._render_spec），作为 LLM 不可用时的降级。"""
    state = ctx.state
    source_refs = ", ".join(s.path for s in state.get("sources", [])[:3]) or "无"
    capabilities = "\n".join(
        f"| BC-{i + 1:03d} | {cap} | P0 | 来自 PRD 和知识库匹配 |"
        for i, cap in enumerate(sub.capabilities)
    ) or "| BC-001 | （待补充） | P0 | 模板生成 |"
    entities = "\n".join(f"| {e} | 子系统拥有或引用的领域对象 |" for e in sub.data_entities) or "| （待补充） | - |"
    return f"""# {sub.name} - 系统需求

> 工作流 ID: {state.get('workflow_id', '')}
> 生成时间: {datetime.now(UTC).isoformat()}
> 知识库引用: {source_refs}

## 1. 执行摘要

| 属性 | 值 |
|------|-----|
| 名称 | {sub.name} |
| 类型 | {sub.type} |
| 职责 | {sub.responsibility} |

## 2. 业务能力

| ID | 能力 | 优先级 | 来源 |
|----|------|--------|------|
{capabilities}

## 3. API 需求

| 方法 | 端点 | 描述 |
|------|------|------|
| GET | /api/v1/{sub.name.replace("-service", "")} | 查询资源列表 |
| POST | /api/v1/{sub.name.replace("-service", "")} | 创建或触发核心业务动作 |
| GET | /api/v1/{sub.name.replace("-service", "")}/{{id}} | 查询资源详情 |

## 4. 数据模型需求

| 实体 | 描述 |
|------|------|
{entities}

## 5. 集成需求

- 同步接口通过 API Gateway 暴露。
- 异步集成建议通过领域事件解耦。
- 关键状态变更需要记录审计日志。

## 6. 验收标准

- [ ] 覆盖核心业务能力。
- [ ] API 输入输出和错误码可被调用方验证。
- [ ] 数据所有权边界清晰。
- [ ] 与相关子系统的同步/异步依赖可追踪。
"""
