---
name: porto
description: |
  Porto - AI 原生 PRD 分解系统。
  将业务需求转换为可执行的子系统规格说明。

  子命令:
  - gen <文件路径> [--name <项目名称>]: 启动新的 PRD 分解工作流
  - continue: 继续当前工作流的下一步
  - resume <工作流ID>: 恢复中断或暂停的工作流
  - status [工作流ID]: 查看工作流详细状态
  - list [选项]: 列出所有 Porto 工作流（支持过滤）

  当用户需要以下操作时使用此技能:
  - 分解 PRD 文档为子系统规格
  - 分析业务需求并识别子系统
  - 生成交互图和子系统上下文
  - 管理 PRD 分解工作流

  触发词: PRD 分解, 需求拆解, 子系统设计, 工作流管理
---

# Porto - PRD 分解系统

AI 原生 PRD 分解系统，通过 4 步交互式工作流将业务需求转换为可执行的子系统规格说明。

## 命令解析

将第一个参数解析为子命令:

```
/porto gen <文件路径> [--name <项目名称>]
/porto continue
/porto resume <工作流ID>
/porto status [工作流ID]
/porto list [选项]
```

| 子命令 | 功能 | 参考文件 |
|--------|------|----------|
| `gen` | 启动新工作流 | 读取 `references/gen.md` |
| `continue` | 继续下一步 | 读取 `references/continue.md` |
| `resume` | 恢复工作流 | 读取 `references/resume.md` |
| `status` | 查看状态 | 读取 `references/status.md` |
| `list` | 列出工作流 | 读取 `references/list.md` |

## 执行流程

1. **解析用户输入**，提取子命令和参数
2. **加载对应的参考文件**:
   - `gen` -> `references/gen.md`
   - `continue` -> `references/continue.md`
   - `resume` -> `references/resume.md`
   - `status` -> `references/status.md`
   - `list` -> `references/list.md`
3. **执行子命令逻辑**

## 核心资源

所有工作流执行使用以下资源（位于此技能内）:

| 资源 | 路径 | 描述 |
|------|------|------|
| Step 1 技能 | `references/prd-decomposition.md` | PRD 分析与理解 |
| Step 2 技能 | `references/subsystem-identification.md` | 子系统识别 |
| Step 3 技能 | `references/subsystem-context-generation.md` | 上下文图生成 |
| Step 4 技能 | `references/subsystem-specification.md` | 规格生成 |
| 知识检索 | `references/knowledge-retrieval.md` | 知识库搜索 |
| 配置文件 | `~/.porto/config.json` | 工作流配置 |
| 工作流存储 | `~/.porto/workflows/` | 所有工作流数据 |

## 4 步工作流

```
Step 1: PRD 分解       -> md/step1_understanding.md
Step 2: 子系统识别     -> md/step2_subsystems.md
Step 3: 上下文生成     -> md/step3_context.md（Mermaid 图表）
Step 4: 规格生成       -> md/step4/{subsystem}/REQUIREMENTS.md
```

## 工作流输出结构

每个工作流生成以下文件:

```
~/.porto/workflows/{workflow_id}/
├── workflow.json
├── current_step
├── inputs/
│   └── {prd_files}
└── md/
    ├── step1_understanding.md
    ├── step2_subsystems.md
    ├── step3_context.md
    └── step4/
        └── {subsystem}/REQUIREMENTS.md
```

## 设计原则

1. **交互式工作流** - 用户可以在每步之间审查和编辑
2. **知识驱动** - 在可用时利用配置的知识库
3. **领域驱动设计** - 使用 DDD 原则进行子系统分解
4. **可追溯性** - 所有输出可追溯到源需求
