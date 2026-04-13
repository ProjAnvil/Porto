---
description: 查看工作流详细状态
---

## 用户输入

```text
$ARGUMENTS
```

## 目标

使用 Python 工作流管理脚本显示特定工作流的详细状态信息。

## 大纲

### Step 1: 解析参数

如果 `$ARGUMENTS` 为空：
```bash
python3 {skills_scripts_dir}/porto_workflow.py status
```
返回活动（最近的 in-progress）工作流。

如果提供工作流 ID 或 `--full`：
```bash
python3 {skills_scripts_dir}/porto_workflow.py status --workflow "{WORKFLOW_ID}"
python3 {skills_scripts_dir}/porto_workflow.py status --workflow "{WORKFLOW_ID}" --full
```

### Step 2: 显示状态报告

解析脚本返回的 JSON 并渲染：

```
═══════════════════════════════════════════════════════════════
📋 工作流状态报告
═══════════════════════════════════════════════════════════════

工作流 ID:     {workflow_id}
项目名称:      {project_name}
创建时间:      {created_at}
最后更新:      {updated_at}
状态:          {status}
进度:          {completed}/{total} 步骤 ({progress_pct}%)

───────────────────────────────────────────────────────────────
📁 工作空间
───────────────────────────────────────────────────────────────

位置: {workspace}

输入文件:
  {input_files_list}

───────────────────────────────────────────────────────────────
📊 步骤进度
───────────────────────────────────────────────────────────────

对 JSON 响应中的每个步骤:

Step {N}: {name}
  状态:   {status_icon} {status}
  输出:   {output} {exists_check}
  大小:   {output_size}
  开始:   {started_at}
  完成:   {completed_at}

───────────────────────────────────────────────────────────────
🔗 快捷操作
───────────────────────────────────────────────────────────────

继续到下一步:
  /porto.continue

稍后恢复此工作流:
  /porto.resume {workflow_id}

查看所有工作流:
  /porto.list
```

### Step 3: 完整预览模式

如果提供 `--full` 参数，JSON 响应包含 `previews` 字段，包含每个输出文件的前 50 行：

```
───────────────────────────────────────────────────────────────
📄 Step 1 预览（前 50 行）
───────────────────────────────────────────────────────────────

{preview content}

───────────────────────────────────────────────────────────────
📄 Step 2 预览（前 50 行）
───────────────────────────────────────────────────────────────

{preview content}
```

### Step 4: 错误处理

**工作流未找到**:
```
❌ 工作流未找到: {workflow_id}

列出可用工作流:
  /porto.list --all
```

**没有活动工作流**:
```
ℹ️ 未找到活动工作流

启动新工作流:
  /porto.gen <prd_file_path>

查看所有工作流:
  /porto.list --all
```

## 备注

- 所有状态由 `skills/scripts/porto_workflow.py` 管理
- 默认显示最近的活动工作流
- `--full` 参数包含内容预览（每个输出的前 50 行）
- 显示文件大小和修改时间
- 包含每个步骤的摘要统计
