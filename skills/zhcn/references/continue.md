---
description: 继续当前工作流的下一步
---

## 用户输入

```text
$ARGUMENTS
```

## 目标

推进当前工作流到下一步。此命令应在审查并可选编辑当前步骤的输出后使用。

## 大纲

### Step 1: 通过 Python 脚本查找当前工作流

```bash
python3 {skills_scripts_dir}/porto_workflow.py current --workflow "{WORKFLOW_ID}"
```

如果不知道工作流 ID，获取活动工作流：

```bash
python3 {skills_scripts_dir}/porto_workflow.py status
```

如果返回 `status: "no_active_workflow"`:
```
❌ 未找到活动工作流

启动新工作流:
  /porto.gen <prd_file_path>

恢复特定工作流:
  /porto.resume <workflow_id>
```

### Step 2: 通过 Python 脚本推进到下一步

```bash
python3 {skills_scripts_dir}/porto_workflow.py advance --workflow "{WORKFLOW_ID}"
```

脚本验证当前步骤已完成，返回下一步信息。

根据 JSON 响应中的 `to_step` 值：

| from_step | to_step | 要执行的 Skill |
|-----------|---------|---------------|
| 1 | 2 | `subsystem-identification` |
| 2 | 3 | `subsystem-context-generation` |
| 3 | 4 | `subsystem-specification` |
| 4 | - | 工作流完成 |

如果返回 `workflow_already_completed`:
```
ℹ️ 工作流已完成

工作流 ID: {WORKFLOW_ID}
状态: completed

查看结果:
  ls ~/.porto/workflows/{WORKFLOW_ID}/step4/

启动新工作流:
  /porto.gen <prd_file_path>
```

### Step 3: 执行 Step 2（如果 to_step = 2）

**Skill**: `subsystem-identification`

1. **标记步骤开始**:
   ```bash
   python3 {skills_scripts_dir}/porto_workflow.py step-start --workflow "{WORKFLOW_ID}" --step 2
   ```

2. **读取前置条件**: `step1_understanding.md`
3. **调用 knowledge-retrieval skill** 检查现有系统
4. **生成**: `step2_subsystems.md`

**生成后，标记完成并记录子系统**:
```bash
python3 {skills_scripts_dir}/porto_workflow.py step-complete \
  --workflow "{WORKFLOW_ID}" \
  --step 2 \
  --output "step2_subsystems.md" \
  --summary '{"subsystems": [{"name": "imed-process", "type": "new"}, ...], "kb_refs": 3}'

python3 {skills_scripts_dir}/porto_workflow.py set-subsystems \
  --workflow "{WORKFLOW_ID}" \
  --subsystems '[{"name": "imed-process", "type": "new"}, ...]'
```

**输出**:
```
═══════════════════════════════════════════════════════════════
✅ Step 2 完成: 子系统识别
═══════════════════════════════════════════════════════════════

📄 输出: ~/.porto/workflows/{WORKFLOW_ID}/step2_subsystems.md

摘要:
• 识别 {N} 个子系统
  - 新建: {n}
  - 扩展现有: {n}
  - 复用现有: {n}
• 匹配 {N} 个知识库引用

已识别子系统:
┌─────────────────┬──────────┬─────────────────────────────┐
│ 名称            │ 类型     │ 职责                        │
├─────────────────┼──────────┼─────────────────────────────┤
│ imed-process    │ new      │ 订单处理                    │
│ ircs-notice     │ extend   │ 多渠道通知                  │
│ payment-gateway │ new      │ 支付集成                    │
└─────────────────┴──────────┴─────────────────────────────┘

───────────────────────────────────────────────────────────────
下一步操作:
───────────────────────────────────────────────────────────────

1. 查看子系统定义:
   cat ~/.porto/workflows/{WORKFLOW_ID}/step2_subsystems.md

2. 如需调整子系统名称或边界（可选）:
   vim ~/.porto/workflows/{WORKFLOW_ID}/step2_subsystems.md

3. 继续到 Step 3（生成交互图）:
   /porto.continue
```

### Step 4: 执行 Step 3（如果 to_step = 3）

**Skill**: `subsystem-context-generation`

1. **标记步骤开始**:
   ```bash
   python3 {skills_scripts_dir}/porto_workflow.py step-start --workflow "{WORKFLOW_ID}" --step 3
   ```

2. **读取前置条件**: `step1_understanding.md`, `step2_subsystems.md`
3. **搜索知识库仓库** 中每个子系统
4. **分析** 代码模式、API、事件
5. **生成**: `step3_context.md`

**生成后标记完成**:
```bash
python3 {skills_scripts_dir}/porto_workflow.py step-complete \
  --workflow "{WORKFLOW_ID}" \
  --step 3 \
  --output "step3_context.md" \
  --summary '{"diagrams": {"sequence": 3, "state": 2, "flowchart": 1, "component": 2, "er": 1}, "kb_repos": 2}'
```

**输出**:
```
═══════════════════════════════════════════════════════════════
✅ Step 3 完成: 子系统上下文生成
═══════════════════════════════════════════════════════════════

📄 输出: ~/.porto/workflows/{WORKFLOW_ID}/step3_context.md

已生成图表:
• {N} 个时序图（业务流程）
• {N} 个状态机（实体状态）
• {N} 个流程图（决策逻辑）
• {N} 个组件图（架构）
• {N} 个 ER 图（数据模型）

已分析知识库仓库:
• imed-process (45 个文件)
• ircs-notice (32 个文件)
• payment-gateway (未找到 - 推断生成)

───────────────────────────────────────────────────────────────
下一步操作:
───────────────────────────────────────────────────────────────

1. 查看交互图:
   cat ~/.porto/workflows/{WORKFLOW_ID}/step3_context.md

2. 如需调整状态机或时序（可选）:
   vim ~/.porto/workflows/{WORKFLOW_ID}/step3_context.md

3. 继续到 Step 4（生成子系统规格）:
   /porto.continue
```

### Step 5: 执行 Step 4（如果 to_step = 4）

**Skill**: `subsystem-specification`

1. **标记步骤开始**:
   ```bash
   python3 {skills_scripts_dir}/porto_workflow.py step-start --workflow "{WORKFLOW_ID}" --step 4
   ```

2. **读取前置条件**: `step1_understanding.md`, `step2_subsystems.md`, `step3_context.md`
3. **检查知识库** 获取参考模式
4. **为每个子系统** 生成 `step4/{subsystem_name}/REQUIREMENTS.md`

**生成后标记完成**:
```bash
python3 {skills_scripts_dir}/porto_workflow.py step-complete \
  --workflow "{WORKFLOW_ID}" \
  --step 4 \
  --output "step4" \
  --summary '{"subsystems": [{"name": "imed-process", "capabilities": 5, "apis": 12}, ...]}'
```

**输出**:
```
═══════════════════════════════════════════════════════════════
✅ Step 4 完成: 子系统规格生成
═══════════════════════════════════════════════════════════════

📁 输出目录: ~/.porto/workflows/{WORKFLOW_ID}/step4/

已生成规格:
┌─────────────────┬────────────────┬─────────────────────────────────┐
│ 子系统          │ 文件           │ 摘要                            │
├─────────────────┼────────────────┼─────────────────────────────────┤
│ imed-process    │ REQUIREMENTS.md│ 5 个能力, 12 个 API, 3 个实体   │
│ ircs-notice     │ REQUIREMENTS.md│ 3 个能力, 8 个 API, 2 个实体    │
│ payment-gateway │ REQUIREMENTS.md│ 4 个能力, 10 个 API, 4 个实体   │
└─────────────────┴────────────────┴─────────────────────────────────┘

═══════════════════════════════════════════════════════════════
🎉 工作流完成！
═══════════════════════════════════════════════════════════════

工作流 ID: {WORKFLOW_ID}

下一步操作:
  • 查看子系统规格:
    cat ~/.porto/workflows/{WORKFLOW_ID}/step4/imed-process/REQUIREMENTS.md

  • 与开发团队分享规格文档

  • 开始实施规划
```

### Step 6: 错误处理

**上一步未完成**（advance 命令错误）:
```
❌ 无法推进: Step {N} 状态是 '{status}'，不是 'completed'。
   请先完成 Step {N}。
```

**缺失前置文件**:
```
❌ 缺失前置文件

期望: ~/.porto/workflows/{WORKFLOW_ID}/step{n-1}_*.md

上一步可能失败了。请尝试:
  /porto.resume {WORKFLOW_ID}
```

**知识库不可用**:
```
⚠️ 警告: 未配置或不可用知识库

将在没有知识库引用的情况下继续。
为获得更好结果，请在 ~/.porto/config.json 中配置知识库

是否继续？（Step 2 将使用通用模式）
```

## 备注

- 所有状态管理由 `skills/scripts/porto_workflow.py` 处理
- 脚本确保幂等操作（崩溃后安全重试）
- 用户可以在继续之前编辑任何步骤的输出
- Step 4 自动标记工作流为完成
