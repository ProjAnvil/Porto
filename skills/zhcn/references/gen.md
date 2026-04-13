---
description: 启动新的 PRD 分解工作流
---

## 用户输入

```text
$ARGUMENTS
```

在继续之前，你**必须**考虑用户输入（如果非空）。

## 目标

初始化一个新的 Porto 工作流并执行 Step 1（业务理解）。工作流是交互式的——每个步骤完成后，用户可以审查并编辑输出，然后继续下一步。

## 大纲

### Step 1: 解析参数并验证

解析 `$ARGUMENTS` 提取：

- **file_paths**（必需）：一个或多个 PRD 文件路径
- **project_name**（可选）：自定义项目名称（`--name` 参数）

如果没有参数：
```
❌ 用法: /porto.gen <文件路径1> [文件路径2 ...] [--name <项目名称>]

说明:
  启动新的 Porto 工作流来分解业务需求

参数:
  <文件路径>       PRD 文档路径
                  支持格式: .md, .pdf, .txt, .docx

选项:
  --name          自定义项目名称（默认：从第一个文件提取）

示例:
  # 单个 PRD 文件
  /porto.gen docs/requirements.md

  # 多个 PRD 文件
  /porto.gen docs/backend_prd.md docs/frontend_prd.md

  # 指定项目名称
  /porto.gen docs/prd.pdf --name "电商平台 v2"
```

**验证**:
1. 验证所有文件存在且可读
2. 如果任何文件缺失，显示错误并列出可用文件

### Step 2: 通过 Python 脚本初始化工作流

使用工作流管理脚本创建工作空间：

```bash
python3 {skills_scripts_dir}/porto_workflow.py init \
  --name "{workflow_name}" \
  --inputs "{file_path1},{file_path2}" \
  --project "{project_name}"
```

脚本将：
- 生成 UUID 工作流 ID
- 在 `~/.porto/workflows/{ID}/` 创建目录结构
- 复制输入文件到 `inputs/`
- 初始化 `workflow.json`，Step 1 状态为 `in_progress`
- 返回工作流 ID 和工作空间路径

解析 JSON 输出，提取 `workflow_id` 和 `workspace`。

**显示初始化信息**:
```
╔═══════════════════════════════════════════════════════════╗
║              Porto 工作流已初始化                          ║
╚═══════════════════════════════════════════════════════════╝

📋 工作流 ID: {WORKFLOW_ID}
📁 工作空间: ~/.porto/workflows/{WORKFLOW_ID}/
📄 项目: {project_name}

输入文件:
  1. {file_name_1}
  2. {file_name_2}

🚀 正在启动 Step 1: 业务需求理解...
```

### Step 3: 执行 Step 1 - 业务理解

加载并执行 `prd-decomposition` skill:

1. **读取输入文件** 从 `inputs/` 目录
2. **分析 PRD** 使用 skill 的指令
3. **生成** `step1_understanding.md`

**重要**: 遵循 `skills/prd-decomposition.md` 中定义的精确输出格式

### Step 4: 通过 Python 脚本标记步骤完成

```bash
python3 {skills_scripts_dir}/porto_workflow.py step-complete \
  --workflow "{WORKFLOW_ID}" \
  --step 1 \
  --output "step1_understanding.md" \
  --summary '{"features": {N}, "p0": {n}, "p1": {n}, "p2": {n}, "hints": {N}, "questions": {N}}'
```

**显示完成信息**:
```
═══════════════════════════════════════════════════════════════
✅ Step 1 完成: 业务需求理解
═══════════════════════════════════════════════════════════════

📄 输出: ~/.porto/workflows/{WORKFLOW_ID}/step1_understanding.md

摘要:
• 识别 {N} 个功能 (P0: {n}, P1: {n}, P2: {n})
• 检测到 {N} 个子系统线索
• 提出 {N} 个待澄清问题

───────────────────────────────────────────────────────────────
下一步操作:
───────────────────────────────────────────────────────────────

1. 查看输出:
   cat ~/.porto/workflows/{WORKFLOW_ID}/step1_understanding.md

2. 如需编辑（可选）:
   vim ~/.porto/workflows/{WORKFLOW_ID}/step1_understanding.md

3. 继续到 Step 2:
   /porto.continue

💡 提示: 你可以在继续之前编辑文档。Porto 将使用
   你编辑后的版本进行下一步。
```

### Step 5: 等待用户操作

Step 1 完成后，**停止并等待** 用户：
- 审查生成的文档
- 如有必要进行编辑
- 运行 `/porto.continue` 进入 Step 2

## 错误处理

**文件未找到**:
```
❌ 错误: 文件未找到

文件: {file_path}

请检查路径后重试。
```

**权限错误**:
```
❌ 错误: 无法创建工作流目录

运行: mkdir -p ~/.porto/workflows && chmod 755 ~/.porto/workflows
```

**无效文件格式**:
```
⚠️ 警告: 文件格式可能不被完全支持

文件: {file_path}
格式: {extension}

继续分析。为获得最佳结果，请使用 .md 或 .txt 文件。
```

## 备注

- 所有状态管理由 `skills/scripts/porto_workflow.py` 处理
- `{skills_scripts_dir}` 解析为 Porto skill 安装目录下的 scripts 目录
- Step 1 skill: `prd-decomposition`
- 用户可以在继续之前编辑任何生成的文件
- 所有输出存储在工作流目录中以便追溯
