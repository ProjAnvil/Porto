from smelt import (
    SmeltCase,
    llm_judge,
    new_case,
    no_tool_call,
    output_contains,
    text,
    tool_call,
    tool_budget,
    turns_used,
)
from smelt.given.agents.llm import LLMClient

def _judge(judge: LLMClient, criteria: str):
    return llm_judge(
        judge,
        criteria=criteria,
        threshold=0.75,
        include_input=True,
        include_trace=True,
    )


def build_cases(judge: LLMClient) -> dict[str, tuple[SmeltCase, ...]]:
    """Construct unbound cases; evaluate_skill binds its current skill to each."""
    return {
        "prd-analysis": (
            new_case("prd-analysis-covers-given-prd")
            .when(
                text(
                    "以下是 PRD 摘要：互联网支付平台需支持支付授权、退款、结算、渠道路由和高风险交易审核，"
                    "并向商户和用户通知结果。请仅基于这段已提供内容提取目标、实体和子系统线索；"
                    "不要读取其他 PRD 或补充摘要没有写的规则。"
                )
            )
            .then(
                output_contains("执行摘要", "业务目标", "核心实体", "子系统线索"),
                _judge(judge, "报告必须覆盖目标、实体和子系统线索，准确区分 PRD 明示内容与未说明内容，不得编造约束。"),
                no_tool_call("get_prd_text"),
                no_tool_call("search_knowledgebase"),
                no_tool_call("search_memory"),
                no_tool_call("get_session_facts"),
                turns_used(max=2),
            ),
            new_case("prd-analysis-fetches-requested-source")
            .when(text("请先读取当前 PRD 原文并检索支付 API 约定，再总结重要需求和技术约束。"))
            .then(
                tool_call("get_prd_text"),
                tool_call("search_knowledgebase"),
                _judge(judge, "回答应使用 PRD 和知识库工具结果中的依据，并标明尚未确定的信息。"),
                no_tool_call("search_memory"),
                no_tool_call("get_session_facts"),
                tool_budget(max=2),
                turns_used(max=4),
            ),
            new_case("prd-analysis-does-not-hijack-translation")
            .when(text("请把这句话翻译成英文：支付成功后，系统通知商户。不要分析业务。"))
            .then(
                _judge(judge, "只翻译用户给出的句子为英文，不生成业务理解报告，也不改变原意。"),
                no_tool_call("get_prd_text"),
                no_tool_call("search_knowledgebase"),
                no_tool_call("search_memory"),
                no_tool_call("get_session_facts"),
                turns_used(max=1),
            ),
            new_case("prd-analysis-keeps-unknown-rules-explicit")
            .when(text("PRD 摘要：支付成功后通知商户。请整理业务目标和子系统线索，并列待澄清问题。"))
            .then(
                output_contains("业务目标", "子系统线索", "待澄清"),
                _judge(judge, "只得出通知能力线索；Kafka、重试次数和时限均未在输入中，必须写为未说明或待澄清，不可当成需求事实。"),
                no_tool_call("get_prd_text"),
                no_tool_call("search_knowledgebase"),
                turns_used(max=2),
            ),
        ),
        "subsystem-decomposition": (
            new_case("decomposition-respects-domain-boundaries")
            .when(
                text(
                    "根据 PRD 拆分支付平台子系统：支付授权、退款、结算、渠道路由、高风险交易审核、"
                    "支付结果通知。给出每个子系统的职责、核心实体和依赖。"
                )
            )
            .then(
                _judge(judge, "划分应有 2–6 个职责清楚的子系统，名称以 -service 结尾，覆盖需求、实体和依赖且边界合理。"),
                no_tool_call("get_prd_text"),
                no_tool_call("search_knowledgebase"),
                no_tool_call("search_memory"),
                no_tool_call("get_session_facts"),
                no_tool_call("list_subsystems"),
                no_tool_call("get_subsystem"),
                turns_used(max=2),
            ),
            new_case("decomposition-does-not-invent-services")
            .when(text("只根据订单创建和支付授权两项能力识别子系统，不要为未出现的能力单独造服务。"))
            .then(
                _judge(judge, "服务数量和职责必须由输入需求支持；不能凭空增加退款、风控或通知服务。"),
                no_tool_call("get_prd_text"),
                no_tool_call("search_knowledgebase"),
                no_tool_call("search_memory"),
                no_tool_call("get_session_facts"),
                no_tool_call("list_subsystems"),
                no_tool_call("get_subsystem"),
                turns_used(max=2),
            ),
            new_case("decomposition-does-not-hijack-ddd-explanation")
            .when(text("请用通俗语言解释限界上下文和聚合根，并各举一个例子。"))
            .then(
                _judge(judge, "用通俗语言解释两个 DDD 概念并举例；不要输出系统拆分表或虚构当前项目服务。"),
                no_tool_call("get_prd_text"),
                no_tool_call("list_subsystems"),
                no_tool_call("get_subsystem"),
                turns_used(max=1),
            ),
            new_case("decomposition-does-not-hijack-user-stories")
            .when(text("根据支付授权和退款这两项能力，各写两个用户故事和一条可验收标准；不要拆服务边界。"))
            .then(
                _judge(judge, "输出四条支付授权/退款用户故事和可验收标准，不要输出子系统划分、服务边界或实体所有权表。"),
                no_tool_call("get_prd_text"),
                no_tool_call("list_subsystems"),
                no_tool_call("get_subsystem"),
                turns_used(max=1),
            ),
            new_case("decomposition-produces-provisional-boundaries-from-visible-scope")
            .when(text("请按职责拆分订单创建、退款和风控审核，说明实体所有权与依赖。源文件 reports/discovery.md 暂不可读取。"))
            .then(
                _judge(judge, "应仅根据明示的订单创建、退款和风控审核给出初步边界、所有权和依赖，并清楚标注报告文件未读取及需确认事项；不能无依据增加服务。"),
                no_tool_call("get_prd_text"),
                no_tool_call("search_memory"),
                turns_used(max=2),
            ),
        ),
        "spec-generation": (
            new_case("spec-generation-emits-verifiable-payment-spec")
            .when(
                text(
                    "为 payment-service 生成规格：商户创建支付单后进行授权，必须支持幂等键；"
                    "支付结果需要查询。包含 API、数据模型、错误码和可测验收标准。"
                    "不需要检索 Porto 约定；对未给出的细节可以写建议值，但必须标记为建议/待确认。"
                )
            )
            .then(
                output_contains("执行摘要", "业务能力", "API 需求", "数据模型需求", "集成需求", "验收标准"),
                _judge(judge, "规格要给出具体 HTTP 端点/方法/输入输出、实体字段及可验证验收标准，不得遗漏幂等约束。未提供的业务规则可作为明确标注的建议或待确认项，不能伪称为 Porto 既有约定。"),
                no_tool_call("get_prd_text"),
                no_tool_call("search_knowledgebase"),
                no_tool_call("search_memory"),
                no_tool_call("get_session_facts"),
                turns_used(max=2),
            ),
            new_case("spec-generation-uses-kb-conventions")
            .when(text("生成 payment-service 的 API 规格；先检索现有支付 API 约定并遵循其路径和错误响应格式。"))
            .then(
                tool_call("search_knowledgebase"),
                _judge(judge, "必须遵循工具结果中的 API 前缀、幂等键和错误响应格式，且不能声称未提供的约定。"),
                no_tool_call("get_prd_text"),
                no_tool_call("search_memory"),
                no_tool_call("get_session_facts"),
                no_tool_call("list_subsystems"),
                no_tool_call("get_subsystem"),
                tool_budget(max=1),
                turns_used(max=4),
            ),
            new_case("spec-generation-marks-proposals-and-unknowns")
            .when(text("为 order-service 写一份六节需求规格：商户创建订单后可查询订单状态。具体字段和错误处理尚未确定。不需要检索 Porto 知识库或约定。"))
            .then(
                output_contains("执行摘要", "业务能力", "API 需求", "数据模型需求", "集成需求", "验收标准"),
                _judge(judge, "完成六节规格草案，至少给出与创建/查询订单对应的具体 API 方案及可测正常路径；字段、错误处理等未给信息要标记建议或待确认，不能伪称为既定规则，也不能只交空模板。"),
                no_tool_call("search_knowledgebase"),
                no_tool_call("search_memory"),
                no_tool_call("get_session_facts"),
                turns_used(max=2),
            ),
            new_case("spec-generation-does-not-hijack-prd-analysis")
            .when(text("请把已有 PRD 的目标用户、业务目标和范围整理成业务理解报告，不要写规格。"))
            .then(
                _judge(judge, "识别这是业务理解而不是子系统规格任务；PRD 原文未提供时必须说明缺少原文并请求用户贴出，不要输出六节规格，也不要只表示以后可以评审。"),
                no_tool_call("search_knowledgebase"),
                no_tool_call("search_memory"),
                turns_used(max=1),
            ),
        ),
        "spec-evaluation": (
            new_case("spec-evaluation-catches-missing-details")
            .when(
                text(
                    "按 Porto rubric 评审以下 payment-service spec，只评审不重写，未提供其他阈值时按 Porto 默认 10/12 判断返工："
                    "POST /payments 创建支付；实体为 Payment；验收标准：系统正常工作。"
                )
            )
            .then(
                output_contains(
                    "需求覆盖度",
                    "API 规范性",
                    "数据模型完整性",
                    "依赖与边界",
                    "可验证性",
                    "一致性",
                    "/12",
                ),
                _judge(judge, "应按六个 rubric 维度引用具体证据并评分；把无法确认的内容标为待确认，不臆造冲突。总分 0–12，并按题目所给 10/12 阈值给出返工结论；不可重写规格。"),
                no_tool_call("get_prd_text"),
                no_tool_call("search_knowledgebase"),
                no_tool_call("search_memory"),
                no_tool_call("get_session_facts"),
                turns_used(max=2),
            ),
            new_case("spec-evaluation-does-not-rewrite")
            .when(text("只评审并给分：支付查询接口 GET /payments/{id} 返回 payment_id 与 status，404 表示不存在。没有另行指定时，按 Porto 默认 10/12 阈值。请勿改写规格。"))
            .then(
                output_contains(
                    "需求覆盖度",
                    "API 规范性",
                    "数据模型完整性",
                    "依赖与边界",
                    "可验证性",
                    "一致性",
                    "/12",
                ),
                _judge(judge, "只依据给出的规格完成六维评审：六维分别评分并引用原文证据，给出总分 /12 和返工结论。不要输出一份替代版或重写后的完整规格。"),
                no_tool_call("get_prd_text"),
                no_tool_call("search_knowledgebase"),
                no_tool_call("search_memory"),
                no_tool_call("get_session_facts"),
                turns_used(max=2),
            ),
            new_case("spec-evaluation-scores-without-upstream-context")
            .when(text("只审核这段规格：POST /orders 创建订单；Order 有 id、status；验收标准是系统正常工作。没有 PRD 或其他上下文。"))
            .then(
                output_contains(
                    "需求覆盖度",
                    "API 规范性",
                    "数据模型完整性",
                    "依赖与边界",
                    "可验证性",
                    "一致性",
                    "/12",
                ),
                _judge(judge, "必须立即按六个维度引用规格证据给分；可核对性未知的覆盖度/一致性应给 1 并说明缺少上游上下文。不得拒绝评审、不得将缺失上下文多次作为缺陷扣分、不得重写或新增强制要求。"),
                no_tool_call("get_prd_text"),
                no_tool_call("search_knowledgebase"),
                no_tool_call("search_memory"),
                turns_used(max=2),
            ),
            new_case("spec-evaluation-does-not-hijack-spec-generation")
            .when(text("帮我写一个支付查询接口的规格，包含请求和响应示例。"))
            .then(
                _judge(judge, "这是规格生成而非评审；不要臆造已有规格并打分，应直接给规格草案或说明缺少的必要输入。"),
                no_tool_call("get_prd_text"),
                no_tool_call("search_knowledgebase"),
                no_tool_call("search_memory"),
                turns_used(max=1),
            ),
        ),
        "porto-memory": (
            new_case("memory-consults-confirmed-decision")
            .when(text("上次我们确认的退款幂等策略是什么？还有哪些相关问题仍待澄清？"))
            .then(
                tool_call("get_session_facts"),
                _judge(judge, "回答应优先引用已确认的退款幂等事实，再指出待澄清项；不能把未决问题当作决定。"),
                no_tool_call("search_memory"),
                no_tool_call("get_prd_text"),
                no_tool_call("list_subsystems"),
                no_tool_call("get_subsystem"),
                tool_budget(max=3),
                turns_used(max=4),
            ),
            new_case("memory-skips-retrieval-for-small-talk")
            .when(text("你好，今天辛苦了！"))
            .then(
                no_tool_call("search_memory"),
                no_tool_call("get_session_facts"),
                no_tool_call("search_knowledgebase"),
                _judge(judge, "自然简短地回应寒暄，不要调用 memory、facts 或 knowledgebase 工具。"),
                turns_used(max=1),
            ),
            new_case("memory-searches-when-session-facts-are-incomplete")
            .when(text("之前讨论过商户对账使用什么外部对账编号？请查一下历史记录。"))
            .then(
                tool_call("get_session_facts"),
                tool_call("search_memory"),
                _judge(judge, "应从历史讨论中回答 merchantReference；不得编造其他规则。"),
                no_tool_call("get_prd_text"),
                no_tool_call("search_knowledgebase"),
                tool_budget(max=2),
                turns_used(max=3),
            ),
            new_case("memory-does-not-hijack-document-questions")
            .when(text("Porto 文档对 API 错误响应格式有什么约定？请查一下文档并说明来源。"))
            .then(
                tool_call("search_knowledgebase"),
                _judge(judge, "从文档检索结果回答 API 错误响应格式并标记文档来源；不要把文档问题当成历史对话检索。"),
                no_tool_call("search_memory"),
                no_tool_call("get_session_facts"),
                no_tool_call("get_prd_text"),
                turns_used(max=2),
            ),
            new_case("memory-distinguishes-document-and-conversation-sources")
            .when(text("我印象里你之前提过 Porto 的接口约定，但不确定是文档里的还是我们聊天里说的。请查一下到底来自哪里，并分别说明找到的内容。"))
            .then(
                tool_call("search_knowledgebase"),
                tool_call("search_memory"),
                _judge(judge, "分别检查知识库文档和会话/历史对话，按来源标明各自检索到的约定；不得把记忆内容说成文档内容，也不能因一个来源有结果就省略用户要求核实的另一个来源。"),
                tool_budget(max=2),
                turns_used(max=5),
            ),
            new_case("memory-does-not-retrieve-answer-already-in-request")
            .when(text("退款请求使用原支付单的幂等键，这条规则已确认。请直接告诉我实现退款幂等时应使用哪个键，不要再查之前的记录。"))
            .then(
                _judge(judge, "直接依据用户当前消息回答退款幂等使用原支付单的幂等键；不要查询 session facts、memory 或 knowledgebase。"),
                no_tool_call("get_session_facts"),
                no_tool_call("search_memory"),
                no_tool_call("search_knowledgebase"),
                turns_used(max=1),
            ),
        ),
    }
