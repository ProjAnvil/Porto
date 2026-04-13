---
description: 恢复中断或暂停的工作流
---

## 用户输入

```text
$ARGUMENTS
```

## 目标

通过 ID 恢复特定工作流。适用于：
- 工作流被中断
- 用户想返回之前的工作流
- 存在多个工作流且用户想切换

## 大纲

### Step 1: 列出可恢复的工作流或恢复特定工作流

如果 `$ARGUMENTS` 为空，列出可恢复的工作流：

```bash
python3 {skills_scripts_dir}/porto_workflow.py resume
```

解析 JSON 响应并显示：

```
用法: /porto.resume <workflow_id>

可恢复的工作流:

┌─────────────┬─────────────────────┬──────────┬───────────┬─────────────┐
│ ID (简短)   │ 项目                │ 步骤     │ 状态      │ 更新时间    │
├─────────────┼─────────────────────┼──────────┼───────────┼─────────────┤
│ a7f3c8b1    │ 电商平台            │ 2/4      │ paused    │ 2024-01-15  │
│ b2e4d6f8    │ 支付网关            │ 1/4      │ in_progr  │ 2024-01-14  │
└─────────────┴─────────────────────┴──────────┴───────────┴─────────────┘

恢复工作流:
  /porto.resume a7f3c8b1
```

如果提供工作流 ID：

```bash
python3 {skills_scripts_dir}/porto_workflow.py resume --workflow "{WORKFLOW_ID}"
```

支持完整 UUID 和简短 ID（前 8 个字符）。

### Step 2: 显示恢复状态

解析 resume 命令的 JSON 响应：

```
═══════════════════════════════════════════════════════════════
📋 恢复工作流: {workflow_id}
═══════════════════════════════════════════════════════════════

项目: {project_name}
状态: {status}

从 Step {resume_from_step} 恢复: {step_name}
Skill: {skill}
前置条件: {prerequisites_ok}

工作空间: {workspace}

───────────────────────────────────────────────────────────────
正在恢复...
───────────────────────────────────────────────────────────────
```

### Step 3: 在适当步骤恢复

根据 JSON 响应中的 `resume_from_step`，调用对应的 skill：

#### 情况 1: Step 1 (understanding)
- 重新执行 `prd-decomposition` skill
- 标记步骤开始：
  ```bash
  python3 {skills_scripts_dir}/porto_workflow.py step-start --workflow "{WORKFLOW_ID}" --step 1
  ```

#### 情况 2: Step 2 (subsystem_identification)
- 加载 Step 1 输出
- 询问用户是先查看 Step 1 还是直接继续
- 如果继续，执行 `subsystem-identification` skill

#### 情况 3: Step 3 (subsystem_context_generation)
- 显示 Step 2 摘要和子系统列表
- 执行 `subsystem-context-generation` skill

#### 情况 4: Step 4 (subsystem_specification)
- 显示 Step 3 摘要
- 执行 `subsystem-specification` skill

#### 情况 5: 工作流已完成
```
ℹ️ 此工作流已完成。

已生成输出位于:
  {workspace}

查看结果:
  ls {workspace}/step4/

启动新工作流:
  /porto.gen <prd_file_path>
```

### Step 4: 错误处理

**工作流未找到**:
```
❌ 工作流未找到: {provided_id}

没有工作流匹配此 ID。

列出可恢复的工作流:
  /porto.resume
```

**前置条件不满足**:
```
⚠️ Step {N} 的前置条件不满足

前序步骤可能有未完成的输出。
检查工作流状态:
  /porto.status {workflow_id}
```

## 备注

- 支持部分 UUID 匹配（最少 8 个字符）
- 使用 `skills/scripts/porto_workflow.py` 管理所有状态
- 恢复前验证工作流完整性
- 恢复时显示进度摘要
- 可以在任何步骤边界恢复
