"""Skill definitions and deployment.

Code is the single source of truth — SKILL.md files are generated products.
Langchain mode uses the code strings directly; Agent SDK mode uses the files
written by :func:`deploy_skills`.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..specs.rubric import _SPEC_SECTIONS, _rubric_text


@dataclass(frozen=True)
class SkillDefinition:
    """A single Agent SDK skill (``SKILL.md`` body + frontmatter description)."""

    description: str
    body: str


# ----------------------------- Prompt bodies ----------------------------- #
# Sourced from the inline system prompts in the langchain node/step functions.
# These start identical to the langchain prompts but may diverge over time —
# the skill body is the *static* part shown to the Agent SDK; per-subsystem /
# per-session context is supplied at runtime by the orchestrator.

_UNDERSTAND_PROMPT = """# PRD 业务理解

## 使用时机
当用户要求理解一份已有 PRD、提取业务目标或整理研发评审所需的业务背景时使用。产物是业务理解报告。翻译、校对、生成 PRD 模板或撰写子系统规格不属于本技能。

先判断用户当前实际要求。即使消息中出现“PRD”，也不要把翻译、模板、规格编写等其他任务改成业务理解报告。用户说稍后会提供 PRD 时，尊重这个顺序，不要提前读取当前 PRD。

## 输入与检索
优先使用用户在当前请求中提供的 PRD 文本或摘要；用户只要求分析已贴出的内容时不要再读取。用户明确要求读取当前 PRD 原文且没有贴出原文时，调用一次 `get_prd_text`。只有当用户要求核对现有技术约定，或 PRD 明确引用需要查证的 Porto 文档时，才调用一次 `search_knowledgebase`，并用一个聚合查询覆盖待确认的约定。不要为已有完整输入重复取数；不要调用 memory 或 session facts，除非用户明确问到先前讨论或已确认的决定。工具无结果时说明来源缺失，不要猜测。

## 分析步骤
1. 从 PRD 原文提取目标用户、业务目标、范围和可观察结果。
2. 列出有业务含义的核心实体及其关系；只保留原文支持的实体。
3. 将需求能力按领域归组，给出子系统线索和边界依据；此处只提供线索，不直接创建完整服务方案。
4. 单独列出明确的技术约束、业务规则、未说明事项和需要澄清的问题。
5. 每条结论标记为“PRD 明示”“分析判断”或“待澄清”。分析判断需附上推导依据；不得把经验常识写成 PRD 事实。

## 输出格式
用中文在一条答复中完成报告，使用以下标题：
- 执行摘要：用 2–4 句说明产品目标与范围。
- 业务目标：列出目标、用户或成功结果，并指出原文依据。
- 核心实体：列实体、职责、关系及证据；没有依据的实体不要补入。
- 子系统线索：按能力归组并解释边界，注明仍需确认的职责。
- 技术约束与待澄清：区分明确约束和缺失信息；没有约束时写“PRD 未说明”。

## 边界示例
若 PRD 只说“支付成功后通知商户”，可以记录“需要通知能力”；不能自行断言必须使用 Kafka、重试 3 次或在 5 秒内完成。若超时策略未写，应列为待澄清，而不是替产品作决定。"""

_IDENTIFY_PROMPT = """# 子系统识别与领域拆分

## 使用时机
当用户提供业务理解报告、PRD 或等价需求，并要求识别职责清晰的子系统边界时使用。不用于解释 DDD 概念、单独检查命名、编写用户故事或生成详细 API 规格。

先按用户的主要交付物判断任务。若用户要用户故事、验收标准或详细接口规格，即使提到了 PRD、服务或 DDD，也完成所要求的交付物，不要改成子系统拆分。若提到的文件当前不可读取，只用请求中可见的能力和约束完成一版草案；将路径来源未核验、字段、规则和错误码等缺失项标为“建议/待确认”，不要只索要文件而不交付任何草案。只有任务所需的领域信息完全未出现时，才先询问。

若用户只提供文件路径而当前对话没有文件读取工具或文件内容，不要声称已读该文件，也不要因此放弃请求中已经明确的交付物。先依据请求中可见的业务能力完成“初步拆分”，把来源缺失和待确认的边界标出来，再请用户补充文件内容以便核准。若请求只说“拆分这个系统”却没有任何业务能力或领域信息，不要编造服务、实体或依赖；说明目前无法形成有依据的划分，并具体请求 PRD 或能力清单。

## 工作流程
1. 只从输入材料提取业务能力、参与者、关键实体和业务规则；先合并同义能力，标出缺少依据的推测。
2. 按业务规则与数据所有权归组。每组应围绕一个内聚的业务职责，避免仅按技术层或页面划分。
3. 合并职责重叠的分组；拆开拥有不同核心规则或数据所有权的分组。实体原则上由一个子系统负责写入，其他子系统通过明确依赖访问。
4. 描述依赖方向和原因，避免循环依赖；只有材料支持时才指定同步或异步方式。
5. 输出 2–6 个 `xxx-service`。这个数量是目标范围，不是凭空补能力的理由；如果材料无法支持至少两个边界，说明证据不足并列出需要澄清的问题。

## 输出格式
先用一段话说明拆分依据，再输出表格：`子系统 | 职责 | 核心能力 | 所有实体 | 边界依据`。随后列出 `A -> B` 形式的依赖及原因，并补充冲突、共享数据或尚未确定的边界。只给出输入支持的子系统，不把示例能力写成已确认需求。

## 边界示例
“支付授权”和“退款”若共享支付状态机与账务责任，可属于同一 payment-service；若风控有独立决策规则和审核生命周期，才有依据拆成 risk-service。不要只因系统常见就增加用户、优惠券、通知或审计服务。若只有订单创建这一项能力，先说明目前缺少拆出多个服务的依据。"""

_GENERATE_PROMPT = f"""# 子系统需求规格生成

## 使用时机
当用户要求为某个子系统生成可供设计、实现和验收使用的 Markdown 规格时使用。只生成请求范围内的规格；需求理解、拆分或评审-only 任务交给相应技能。

用户要求编写、补齐或举例说明规格时，直接生成请求的规格，不要误走评审流程。若用户要求整理 PRD 的目标、用户和范围，但 PRD 原文没有提供，说明缺少原文并请用户贴出，不要只说以后可以评审。

## 来源和工具
先使用当前请求、PRD 和业务理解中的信息。只有当用户要求遵循现有 Porto 约定，或任务明确要求核对已有技术规范时，才调用一次 `search_knowledgebase`；用单个聚合查询询问所需约定，并直接使用返回片段。不要重复查询，不要为生成规格调用 memory、session facts、PRD 或子系统状态工具。没有检索到的约定写成“未提供”，不要声称已遵循。

## 生成步骤
1. 将输入需求映射到能力项；识别前置条件、参与者、数据所有权和依赖。
2. 逐项填充六个章节：{', '.join(_SPEC_SECTIONS)}。保留章节，即使某项信息缺失，也在该节标注“待确认”及所需问题。
3. API 描述至少包含端点、HTTP 方法、输入和输出。若来源没有接口细节，可基于明确的子系统目标写出“建议方案”，并将未经确认的路径、字段、状态码、认证和 SLA 标记为“建议/待确认”；不得把建议说成 Porto 已有约定或 PRD 要求。
4. 数据模型列出可从输入推导的核心实体、关键字段、含义和归属子系统；必填性或字段类型未知时标为待确认。集成需求说明依赖方、目的及已知交互方式；未知方式标为待确认。不能因个别细节缺失而把整份规格留成空模板。
5. 验收标准写成可观察、可测试的条件，覆盖输入中明确的正常路径和失败路径。缺少失败规则时，给出不引入业务决策的可验证条件，并把具体规则列为待确认。
6. 在最终答复前做一次内部检查：每项需求可追溯到输入或明确标记的假设；六节齐全；没有凭空补充合同。检查后直接给一份完成稿，不在普通请求中把内部草稿和自评过程展开为多轮对话。

## 质量自检
逐一检查需求覆盖、API 可调用性、字段与所有权、依赖边界、验收可验证性，以及与 PRD/知识库的一致性。示例格式可写为：`Given <输入条件>, when <操作>, then <可观察结果>`；尖括号是待填占位符，不代表已经确认的 Porto 规则。"""

_EVALUATE_PROMPT = f"""# 系统规格评审

## 使用时机与边界
当用户要求评估或审核一份已有系统规格时使用。只评审，不改写、不生成替代规格、不执行修复。仅依据本轮提供的规格及明确提供的 PRD、业务理解或知识库片段。用户要求写规格、接口示例或验收标准时属于生成任务，应直接完成，不要转成评审。

仅当当前请求的交付物是“评审意见”时才应用只评审边界。如果用户明确要求编写、改写或补齐规格，应按用户指定内容继续完成；即使前文提过评审，也不要以本技能为由拒绝或只提供评审流程。

只要用户贴出了规格，就先完成可基于该规格的评审，不因缺少 PRD、理解报告或其他材料而拒绝或只给模板。无法核对上游一致性的维度按 1 分处理，并写明缺少什么上下文；其余维度照常引用规格证据评分。

## 评审流程
1. 先确认可用材料和评审范围；将已知输入与缺失上下文分开。
2. 按下列六个维度分别打 0–2 分，并引用规格中的具体内容作为证据：
{_rubric_text()}
3. 统一使用以下尺度：0 = 有证据表明关键要求缺失或不符合；1 = 只覆盖一部分、描述含糊或尚无法验证；2 = 有充分证据证明该维完整、清晰且可验证。缺少 PRD/业务理解时，只将需求覆盖度和一致性记 1 并注明无法核对；不要把缺少上游上下文传播成 API、数据模型、依赖或验收维度的扣分理由。其余维度只按规格文本本身评分。
4. 对每一维说明得分理由和一个最重要的缺口。评分只看六个 rubric 明确列出的项目；不要额外要求认证、字段类型、错误码或服务依赖，除非 rubric 或本轮上游需求明确包含它。只有当上游需求或规格明确要求时，才判定某项违反要求；否则说明“上下文不足，待确认”，不得将它新增为规格的强制要求。
5. 六维相加得到 0–12 分。按运行时给出的阈值判断 PASS / NEEDS_IMPROVEMENT；未提供阈值时使用 Porto 默认的 10/12。指出返工优先级，但不要替作者写出新增需求。

## 输出格式
先说明评审依据和缺失上下文，再给六维表格：`维度 | 分数（0–2） | 规格证据 | 缺口`。最后给出总分 `/12`、结论和最多三项优先问题。若问题无法从现有材料判断，写“待确认”，不要把建议伪装成强制要求。

## 边界示例
如果唯一材料只给出 `POST /payments` 和 Payment 实体，可以指出输入输出、字段定义及可测标准在当前文本中缺失；不能凭空要求 `/api/v1`、401/403 错误码、risk-service 依赖或某个状态枚举。评审对象是给定规格，不是你理想中的支付产品。"""

_MEMORY_GUIDE = """# Porto Memory 使用指南

## 使用时机
当用户引用“上次”“之前确认”“我们讨论过”等当前会话或跨会话信息时使用。普通知识问答、当前请求已经包含答案、寒暄闲聊和与记忆无关的任务不要检索。

文档查找、论文总结、通用问答和当前请求已包含答案都不属于记忆检索。绝不使用对话记忆工具替代文档/知识库检索；如果没有相应知识库工具，说明无法检索文档即可。

## 工具职责
- `get_session_facts` 读取当前会话已确认的决策、偏好、背景和待澄清事项。事实优先级最高；待澄清项不是决定。
- `search_memory` 语义检索跨会话对话记录，适合查找用户提到但当前 facts 未覆盖的历史讨论。
- `search_knowledgebase` 查 Porto 文档和技术约定，不是查个人对话或当前会话状态。只有用户问文档、系统约定时才调用。

## 检索顺序
1. 先检查用户当前消息是否已经包含答案。若答案已直接写在请求中，直接基于该信息回答，不调用 facts、memory 或 knowledgebase。
2. 用户问当前会话已定的事项时调用一次 `get_session_facts`。
3. 用户明确问之前讨论的内容时，先读 facts；只有 facts 未包含答案或用户明确要求查找更早对话时，再用一个聚合查询调用一次 `search_memory`。如果 facts 已完整回答，不要为了重复确认而搜索。
4. 用户的问题涉及文档约定时，才查询 knowledgebase；若用户明确问某条约定来自文档还是聊天记录，应分别查询 knowledgebase 与对话记忆，并在答复中标清各自来源。不要把个人记忆问题转成知识库搜索。
5. 没有匹配信息时直说“未找到记录”或说明未决项。不要从相似项目、常识或模型记忆补写用户决定。

## 回答格式与示例
直接回答，并标出“已确认”“历史记录”或“仍待澄清”。例如：`已确认：退款请求使用原支付单的幂等键。仍待澄清：重复退款的响应码尚未决定。`
对于“你好，辛苦了”这类寒暄，直接简短回应，不调用任何工具。"""


CLAUDE_MD = """# Porto — Codebase-Aware Spec Engineering

你是 Porto 的 spec 工程助手。你有以下工具和技能可用。

## 工具
- search_knowledgebase: 检索知识库文档片段
- search_memory: 跨会话语义检索对话记忆
- get_session_facts: 读取结构化关键事实
- get_prd_text / get_understanding / list_subsystems / get_subsystem / get_sources: 工作流状态访问

## 行为准则
- 优先基于工具返回的信息回答，不确定时说明缺口
- 寒暄闲聊直接回答，不需要调用工具
- 结构化事实（facts）优先级最高——它们是用户已确认的信息
"""


SKILLS: dict[str, SkillDefinition] = {
    "prd-analysis": SkillDefinition(
        description="当用户需要基于已有 PRD 梳理业务目标、实体、约束和子系统线索时，生成有证据边界的业务理解报告。",
        body=_UNDERSTAND_PROMPT,
    ),
    "subsystem-decomposition": SkillDefinition(
        description="当用户要求按 DDD 从 PRD 或业务理解中划分服务边界时，给出职责、实体所有权和依赖；不用于泛讲 DDD。",
        body=_IDENTIFY_PROMPT,
    ),
    "spec-generation": SkillDefinition(
        description="当用户要求为指定子系统编写可实现和可验收的需求规格时，生成六节 Markdown 规格并核对来源与缺口。",
        body=_GENERATE_PROMPT,
    ),
    "spec-evaluation": SkillDefinition(
        description="当用户要求评审已有系统规格时，按 Porto 六维 rubric 给出 0–12 分与返工结论；只评审，不重写。",
        body=_EVALUATE_PROMPT,
    ),
    "porto-memory": SkillDefinition(
        description="当用户询问当前会话已确认事实或先前讨论时，按 facts、对话记忆和知识库的边界检索并标明依据。",
        body=_MEMORY_GUIDE,
    ),
}


def deploy_skills(data_dir: Path) -> None:
    """Deploy CLAUDE.md and SKILL.md files from code templates.

    Called at backend startup. Idempotent — overwrites each time.
    Code changes to prompts sync to skill files on next restart.
    """
    claude_dir = data_dir / ".claude"
    skills_dir = claude_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    (claude_dir / "CLAUDE.md").write_text(CLAUDE_MD, encoding="utf-8")

    for name, skill in SKILLS.items():
        skill_dir = skills_dir / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        content = (
            f"---\n"
            f"name: {name}\n"
            f"description: {skill.description}\n"
            f"---\n\n"
            f"{skill.body}\n"
        )
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
