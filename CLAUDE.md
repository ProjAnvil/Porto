# Porto - Claude Code 项目指令

## 项目概述

Porto 是一个 AI 原生的 PRD（产品需求文档）分解系统，将业务需求转换为可执行的子系统规格说明。

## 核心工作流

```
Step 1: PRD 分解       → step1_understanding.md
Step 2: 子系统识别     → step2_subsystems.md
Step 3: 上下文生成     → step3_context.md（Mermaid 图表）
Step 4: 规格生成       → step4/{subsystem}/REQUIREMENTS.md
```

## 文件结构

```
Porto/
├── CLAUDE.md                           # 本文件
├── README.md                           # 用户文档
├── install.sh                          # 安装脚本（支持 --lang=en/zhcn）
├── config.example.json                 # 配置模板
├── porto_server.py                     # HTTP 工作流查看器服务
├── agents/                             # 子代理
│   ├── en/
│   │   └── prd-analyst.md             # PRD 分析专家
│   └── zhcn/
│       └── prd-analyst.md             # PRD 分析专家（中文）
└── skills/                             # 技能入口 + 参考文档
    ├── en/
    │   ├── SKILL.md                    # 技能入口（子命令路由）
    │   └── references/                 # 参考文档
    │       ├── gen.md                  # /porto gen 命令实现
    │       ├── continue.md             # /porto continue 命令实现
    │       ├── resume.md               # /porto resume 命令实现
    │       ├── status.md               # /porto status 命令实现
    │       ├── list.md                 # /porto list 命令实现
    │       ├── prd-decomposition.md    # Step 1 技能
    │       ├── subsystem-identification.md   # Step 2 技能
    │       ├── subsystem-context-generation.md  # Step 3 技能
    │       ├── subsystem-specification.md    # Step 4 技能
    │       └── knowledge-retrieval.md  # 知识库检索
    └── zhcn/
        ├── SKILL.md
        └── references/
            └── （同上，中文版）
```

## 安装后目录结构

```
~/.claude/
├── skills/
│   └── porto/                          # 技能符号链接
│       ├── SKILL.md → {project}/skills/{lang}/SKILL.md
│       └── references/ → {project}/skills/{lang}/references/
└── agents/
    └── prd-analyst.md → {project}/agents/{lang}/prd-analyst.md

~/.porto/
├── config.json                         # 工作流配置
├── server.py                           # HTTP 工作流查看器（安装时从项目根目录复制）
└── workflows/
    └── {uuid}/
        ├── workflow.json
        ├── current_step
        ├── inputs/
        ├── step1_understanding.md
        ├── step2_subsystems.md
        ├── step3_context.md
        └── step4/
            └── {subsystem}/REQUIREMENTS.md
```

## 开发规范

### 双语支持

- 安装时通过 `--lang=en` 或 `--lang=zhcn` 选择语言
- 默认语言为英文 (en)
- 语言目录命名：`en`（英文）、`zhcn`（中文）

### 修改 Skills

- SKILL.md 是技能入口，负责子命令路由
- `references/` 目录存放所有参考文档（命令实现和步骤技能）
- 修改输出格式时，同步更新：
  - `skills/{lang}/references/subsystem-specification.md`
  - `skills/{lang}/references/continue.md`
  - `README.md`
  - **同时更新 en 和 zhcn 两个版本**

### 修改 Commands

- 命令实现位于 `skills/{lang}/references/` 下
- SKILL.md 负责子命令路由（gen, continue, resume, status, list）
- 错误消息格式使用 emoji 前缀：`❌` `⚠️` `✅` `📋`
- **修改时同时更新 en 和 zhcn 两个版本**

### 添加新子命令

1. 在 `skills/en/references/` 创建 `{command}.md`
2. 在 `skills/zhcn/references/` 创建对应的中文版本
3. 更新 `skills/en/SKILL.md` 和 `skills/zhcn/SKILL.md` 的命令路由表
4. 更新 `install.sh` 的摘要部分
5. 更新 README.md

### 输出文件命名

| 文件 | 命名 |
|------|------|
| Step 1 输出 | `step1_understanding.md` |
| Step 2 输出 | `step2_subsystems.md` |
| Step 3 输出 | `step3_context.md` |
| Step 4 输出 | `step4/{subsystem}/REQUIREMENTS.md` |

## 知识库依赖

Porto 可选依赖配置的知识库（通过 `~/.porto/config.json` 配置）：

- 知识库存在时：参考现有系统模式和约定
- 知识库不存在时：使用通用最佳实践
