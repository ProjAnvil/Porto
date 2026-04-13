---
description: 列出所有 Porto 工作流（支持过滤）
---

## 用户输入

```text
$ARGUMENTS
```

## 目标

使用 Python 工作流管理脚本查询并显示 Porto 系统中的所有工作流。

## 大纲

### Step 1: 解析参数并调用脚本

将 `$ARGUMENTS` 映射到脚本参数：

| 用户参数 | 脚本命令 |
|---------|---------|
| (无) | `python3 {scripts}/porto_workflow.py list` |
| `--all` | `python3 {scripts}/porto_workflow.py list --all` |
| `--recent <days>` | `python3 {scripts}/porto_workflow.py list --recent {days}` |
| `--status <status>` | `python3 {scripts}/porto_workflow.py list --status {status}` |
| `--name <keyword>` | `python3 {scripts}/porto_workflow.py list --name {keyword}` |
| `--step <n>` | `python3 {scripts}/porto_workflow.py list --step {n}` |
| `<workflow_id>` | `python3 {scripts}/porto_workflow.py status --workflow {id}` |

如果参数无效：
```
用法: /porto.list [选项] [workflow_id]

模式:
  /porto.list                           列出最近 3 天的工作流（默认）
  /porto.list --recent <days>           列出最近 N 天的工作流
  /porto.list --all                     列出所有工作流
  /porto.list <workflow_id>             显示特定工作流详情
  /porto.list --status <status>         按状态过滤
  /porto.list --name <keyword>          按项目名称过滤
  /porto.list --step <n>                按当前步骤过滤（1/2/3/4）

示例:
  /porto.list                           # 最近 3 天
  /porto.list --recent 7                # 最近 7 天
  /porto.list a7f3c8b1                  # 特定工作流
  /porto.list --status in_progress      # 仅进行中的
  /porto.list --step 2                  # Step 2 的工作流
```

### Step 2: 显示列表视图

解析脚本的 JSON 输出并渲染：

```
╔═══════════════════════════════════════════════════════════════╗
║                    Porto 工作流                               ║
╚═══════════════════════════════════════════════════════════════╝

显示: 最近 3 天 | 共 {total} 个工作流

┌──────────────┬───────────────────────┬──────┬───────────┬────────────┐
│ ID (简短)    │ 项目                  │ 步骤 │ 状态      │ 创建时间   │
├──────────────┼───────────────────────┼──────┼───────────┼────────────┤
│ a7f3c8b1     │ 电商平台              │ 4/4  │ ✅ 完成   │ 2024-01-15 │
│ b2e4d6f8     │ 支付网关              │ 2/4  │ 🔄 进行中 │ 2024-01-14 │
│ c5g7h9i1     │ 库存系统              │ 1/4  │ ⏸️ 暂停   │ 2024-01-13 │
└──────────────┴───────────────────────┴──────┴───────────┴────────────┘

状态图例:
  ✅ 已完成  🔄 进行中  ⏸️ 暂停  ❌ 失败

统计:
  总计: {total} | 已完成: {completed} | 进行中: {in_progress} | 暂停: {paused} | 失败: {failed}

命令:
  查看详情:     /porto.status <id>
  恢复工作流:   /porto.resume <id>
  新建工作流:   /porto.gen <prd_file>
```

### Step 3: 显示特定工作流（提供 workflow_id 时）

如果提供了 workflow_id，脚本返回详细状态，按 `status.md` 中描述的方式渲染。

### Step 4: 空状态

如果没有找到工作流：
```
📁 未找到工作流

创建第一个工作流:
  /porto.gen <prd_file_path>

示例:
  /porto.gen docs/requirements.md
```

## 状态图标

| 状态 | 图标 |
|------|------|
| `completed` | ✅ |
| `in_progress` | 🔄 |
| `paused` | ⏸️ |
| `failed` | ❌ |

## 备注

- 所有数据来自 `skills/scripts/porto_workflow.py`
- 工作流 ID 可以是部分的（最少 8 个字符）
- 默认显示最近 3 天
- 列表视图包含统计摘要
