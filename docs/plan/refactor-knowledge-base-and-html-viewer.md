# Porto 重构计划：知识库泛化 + 静态 HTML 工作流查看器

> 日期：2026-04-11
> 状态：Draft

---

## 一、背景与目标

### 1.1 当前问题

1. **SCV 强耦合**：Porto 当前硬编码依赖 SCV（Source Code Vault）作为唯一知识库来源，包括：
   - 配置文件中的 `scv_knowledge_base`、`scv_templates` 字段
   - Skill 描述中频繁出现 `~/.scv/analysis/`、`~/.scv/repos/` 等硬编码路径
   - `knowledge-retrieval.md` 整个 skill 完全围绕 SCV 设计
   - `subsystem-context-generation.md` 中直接用 shell 命令搜索 SCV 仓库
   - `subsystem-specification.md` 中引用 SCV 分析结果路径

2. **工作流输出缺乏可视化**：每步产出仅为 markdown 文件，缺乏全局视图和交互式浏览/编辑能力。

### 1.2 改造目标

1. **知识库泛化（Knowledge Base Abstraction）**：将 "SCV" 概念替换为 "Knowledge Base"，支持多个可插拔的知识库源（SCV 只是其中之一）。
2. **静态 HTML 工作流查看器**：通过 Python 脚本生成静态 HTML + JSON，提供时间轴式的工作流全局视图，支持查看和编辑每步的 markdown 产出。

---

## 二、任务 1：知识库泛化

### 2.1 新的知识库概念

将 SCV 的单一绑定替换为一个 `knowledge_bases` 列表，每个知识库包含：

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | 知识库唯一标识符（如 `"scv"`、`"local-docs"`、`"api-specs"`） |
| `type` | string | 知识库类型：`directory`（本地目录）、`db`（数据库，暂不支持） |
| `path` | string | 知识库根路径 |
| `description` | string | 用途描述 |
| `enabled` | bool | 是否启用 |

### 2.2 配置文件变更

**文件**：`config.example.json`

```jsonc
{
  "porto_home": "~/.porto",
  "workflows_dir": "~/.porto/workflows",
  "language": "en",

  // 替换原来的 scv_knowledge_base 和 scv_templates
  "knowledge_bases": [
    {
      "name": "scv",
      "type": "directory",
      "path": "~/.scv/analysis",
      "repos_path": "~/.scv/repos",
      "description": "SCV codebase analysis knowledge base",
      "enabled": true
    }
    // 用户可添加更多知识库：
    // {
    //   "name": "internal-docs",
    //   "type": "directory",
    //   "path": "~/company/architecture-docs",
    //   "description": "Internal architecture documentation",
    //   "enabled": true
    // }
    //
    // db 类型暂不支持：
    // {
    //   "name": "vector-store",
    //   "type": "db",
    //   "path": "...",
    //   "description": "Vector database knowledge base",
    //   "enabled": false
    // }
  ],

  "knowledge_retrieval": {
    "enabled": true,
    "match_strategy": "hybrid",
    "similarity_threshold": 0.6,
    "max_references": 5
  },

  "workflow": {
    "steps": [
      {
        "id": 1,
        "name": "understanding",
        "description": "Read and understand business requirements",
        "output_file": "step1_understanding.md",
        "skill": "prd-decomposition"
      },
      {
        "id": 2,
        "name": "subsystem_identification",
        "description": "Identify subsystems and their responsibilities",
        "output_file": "step2_subsystems.md",
        "skill": "subsystem-identification"
      },
      {
        "id": 3,
        "name": "subsystem_spec_generation",
        "description": "Generate detailed specifications for each subsystem",
        "output_dir": "step3",
        "skill": "subsystem-spec-generation"
      }
    ]
  }
}
```

**关键变更**：
- 移除 `scv_knowledge_base` 和 `scv_templates` 字段
- 新增 `knowledge_bases` 数组，支持多个知识库配置
- 保留 `knowledge_retrieval` 配置不变

### 2.3 需要修改的文件清单

以下所有文件需要将 SCV 硬编码引用替换为泛化的知识库概念：

| # | 文件 | 变更类型 | 变更内容 |
|---|------|----------|----------|
| 1 | `config.example.json` | 结构重构 | 替换 `scv_*` 为 `knowledge_bases` 数组 |
| 2 | `skills/en/SKILL.md` | 文字更新 | 将 "SCV Knowledge" 改为 "Knowledge Base"；移除 `~/.scv/` 硬编码路径 |
| 3 | `skills/zhcn/SKILL.md` | 文字更新 | 同上（中文版） |
| 4 | `skills/en/references/knowledge-retrieval.md` | 大幅重写 | 从"SCV检索"改为"多知识库通用检索"；支持按配置遍历多个知识库 |
| 5 | `skills/zhcn/references/knowledge-retrieval.md` | 大幅重写 | 同上（中文版） |
| 6 | `skills/en/references/subsystem-identification.md` | 文字更新 | 将 "SCV analysis" 改为 "knowledge base" |
| 7 | `skills/zhcn/references/subsystem-identification.md` | 文字更新 | 同上（中文版） |
| 8 | `skills/en/references/subsystem-context-generation.md` | 大幅重写 | 将 SCV 仓库搜索逻辑改为按配置的知识库搜索 |
| 9 | `skills/zhcn/references/subsystem-context-generation.md` | 大幅重写 | 同上（中文版） |
| 10 | `skills/en/references/subsystem-specification.md` | 文字更新 | 将 SCV 引用改为通用知识库引用 |
| 11 | `skills/zhcn/references/subsystem-specification.md` | 文字更新 | 同上（中文版） |
| 12 | `skills/en/references/continue.md` | 文字更新 | 如有 SCV 引用则替换 |
| 13 | `skills/zhcn/references/continue.md` | 文字更新 | 同上（中文版） |
| 14 | `README.md` | 文字更新 | 重写"Knowledge Base Integration"章节 |
| 15 | `docs/README_zhcn.md` | 文字更新 | 重写"知识库集成"章节 |
| 16 | `CLAUDE.md` | 文字更新 | 将 SCV 依赖描述改为可选知识库 |
| 17 | `agents/en/prd-analyst.md` | 文字更新 | 若有 SCV 引用则替换 |
| 18 | `agents/zhcn/prd-analyst.md` | 文字更新 | 同上（中文版） |

### 2.4 知识库检索逻辑设计

重写后的 `knowledge-retrieval.md` 核心逻辑：

```
1. 读取 ~/.porto/config.json 中的 knowledge_bases 配置
2. 过滤出 enabled=true 的知识库
3. 对每个知识库，根据 type 选择检索策略：
   a. type=directory → 搜索 {path}/ 下的 markdown/文档文件、代码文件等
   b. type=db        → （暂不支持，跳过并提示）
4. 合并所有知识库的检索结果
5. 按相关性排序，返回 top N 条
```

### 2.5 替换规则

| 原文（EN） | 替换为（EN） |
|------------|-------------|
| `SCV knowledge base` | `knowledge base` |
| `SCV analysis` | `knowledge base analysis` |
| `SCV code repositories` | `code repositories in knowledge base` |
| `~/.scv/analysis/` | (根据配置动态读取) |
| `~/.scv/repos/` | (根据配置动态读取) |
| `/scv.gather` | (移除或改为通用描述) |
| `/scv.run` | (移除或改为通用描述) |
| `/scv.batchRun` | (移除或改为通用描述) |
| `SCV Knowledge` (表格行) | `Knowledge Bases` |

| 原文（ZH） | 替换为（ZH） |
|------------|-------------|
| `SCV 知识库` | `知识库` |
| `SCV 代码仓库` | `知识库中的代码仓库` |
| `SCV 分析` | `知识库分析` |

---

## 三、任务 2：静态 HTML 工作流查看器

### 3.1 目标

用一个 Python 脚本（`porto_viewer.py`）为每个 workflow 生成一套静态 HTML + JSON 文件，用户可在浏览器中：

- 通过**左侧时间轴**浏览每一步骤
- 查看每一步的**输入**（PRD 原文）和**输出**（AI 解析结果 markdown）
- **编辑** markdown 内容并保存（浏览器端编辑 → 保存到本地 JSON/MD 文件）
- 对于 Step 2，可**修改/增删**识别出的子系统
- 对于 Step 3，可单击子系统名称查看/编辑对应的 REQUIREMENTS.md

### 3.2 工作流目录结构设计

将 markdown 产出统一放入 `md/` 文件夹，`index.html` 和 `data/` 直接位于 workflow 根目录，消除原先根目录散落 md 文件与 viewer 副本的冗余：

```
~/.porto/workflows/{workflow_id}/
├── workflow.json                    # 工作流状态（元数据）
├── current_step                     # 当前步骤号
├── inputs/                          # PRD 原文
│   └── requirements.md
├── index.html                       # HTML 查看器入口
├── data/                            # JSON 结构化数据层
│   ├── step1.json                   # Step 1 结构化数据
│   ├── step2.json                   # Step 2 结构化数据（含子系统列表）
│   └── step3.json                   # Step 3 结构化数据（含每子系统 spec）
└── md/                              # 所有 Markdown 产出（唯一存放位置）
    ├── step1_understanding.md       # Step 1 产出
    ├── step2_subsystems.md          # Step 2 产出
    └── step3/                       # Step 3 产出（每子系统一个目录）
        ├── {subsystem_1}/
        │   └── REQUIREMENTS.md
        └── {subsystem_2}/
            └── REQUIREMENTS.md
```

**关键变更**：
- **取消 `viewer/` 层级**：`index.html`、`data/` 直接在 workflow 根目录
- **`md/` 是 markdown 唯一存放位置**：所有步骤的产出直接写入 `md/`，不再在根目录散落 `step1_understanding.md` 等文件
- **HTML 查看器直接读取 `md/`**：无需复制，消除数据同步问题
- **Step 3 保留子目录结构**：`md/step3/{subsystem}/REQUIREMENTS.md`，与现有命名一致

**需同步修改的引用**：
- `porto_workflow.py` 中 `STEP_DEFINITIONS` 的 `output_file` / `output_dir` 需加 `md/` 前缀
- 各 skill（`gen.md`、`continue.md`、`prd-decomposition.md` 等）中写入文件的路径
- `SKILL.md` 和 `README.md` 中的输出结构示例

### 3.3 JSON 数据格式设计

#### step1.json

```json
{
  "step": 1,
  "name": "Business Understanding",
  "status": "completed",
  "started_at": "2026-04-11T10:00:00Z",
  "completed_at": "2026-04-11T10:05:00Z",
  "input_files": ["requirements.md"],
  "output_file": "step1_understanding.md",
  "content": {
    "markdown_file": "md/step1_understanding.md",
    "sections": [
      {
        "id": "project-background",
        "title": "Project Background",
        "content_preview": "..."
      },
      {
        "id": "business-objectives",
        "title": "Business Objectives",
        "content_preview": "..."
      }
    ]
  }
}
```

#### step2.json

```json
{
  "step": 2,
  "name": "Subsystem Identification",
  "status": "completed",
  "started_at": "2026-04-11T10:10:00Z",
  "completed_at": "2026-04-11T10:15:00Z",
  "output_file": "step2_subsystems.md",
  "content": {
    "markdown_file": "md/step2_subsystems.md",
    "subsystems": [
      {
        "name": "imed-process",
        "type": "extend",
        "responsibility": "Order processing and management",
        "capabilities": ["Order creation", "Order tracking"],
        "dependencies": ["payment-gateway", "ircs-notice"],
        "kb_reference": "scv:imed-process"
      },
      {
        "name": "ircs-notice",
        "type": "new",
        "responsibility": "Notification service",
        "capabilities": ["Email", "SMS", "Push"],
        "dependencies": [],
        "kb_reference": null
      }
    ]
  }
}
```

#### step3.json

```json
{
  "step": 3,
  "name": "Subsystem Specification",
  "status": "completed",
  "started_at": "2026-04-11T10:20:00Z",
  "completed_at": "2026-04-11T10:30:00Z",
  "output_dir": "step3",
  "content": {
    "subsystems": [
      {
        "name": "imed-process",
        "markdown_file": "md/step3/imed-process/REQUIREMENTS.md",
        "sections": ["Executive Summary", "Business Capabilities", "API Requirements", "Data Model"],
        "api_count": 5,
        "entity_count": 3
      },
      {
        "name": "ircs-notice",
        "markdown_file": "md/step3/ircs-notice/REQUIREMENTS.md",
        "sections": ["Executive Summary", "Business Capabilities", "API Requirements"],
        "api_count": 3,
        "entity_count": 2
      }
    ]
  }
}
```

### 3.4 HTML 页面设计

单页应用（纯静态，无需后端），技术栈：

- **HTML5 + CSS3 + Vanilla JS**（零依赖，无需 npm）
- **Marked.js** 或内联简易 markdown 渲染器（CDN 引入）
- **Mermaid.js**（CDN 引入，渲染 Step 3 中的图表）

页面布局：

```
┌────────────────────────────────────────────────────────┐
│  Porto Workflow Viewer - {project_name}                │
├──────────────┬─────────────────────────────────────────┤
│              │                                         │
│  Timeline    │   Content Panel                         │
│              │                                         │
│  ● Step 1    │   ┌─────────────────────────────────┐  │
│    Understand│   │  Step 1: Business Understanding  │  │
│    ✓ Done    │   │                                   │  │
│              │   │  [View] [Edit]                     │  │
│  ● Step 2    │   │                                   │  │
│    Identify  │   │  Rendered Markdown Content...      │  │
│    ✓ Done    │   │                                   │  │
│              │   │                                   │  │
│  ● Step 3    │   └─────────────────────────────────┘  │
│    Specify   │                                         │
│    In Prog   │   For Step 2: Subsystem Cards          │
│              │   ┌────────┐ ┌────────┐ ┌────────┐    │
│              │   │imed-   │ │ircs-   │ │payment-│    │
│              │   │process │ │notice  │ │gateway │    │
│              │   └────────┘ └────────┘ └────────┘    │
│              │                                         │
│              │   For Step 3: Click subsystem → spec   │
│              │                                         │
├──────────────┴─────────────────────────────────────────┤
│  Footer: Workflow ID | Created | Status                │
└────────────────────────────────────────────────────────┘
```

#### 交互设计

1. **时间轴点击** → 右侧面板切换到对应步骤的内容
2. **View 模式** → markdown 渲染为 HTML（含 Mermaid 图表渲染）
3. **Edit 模式** → 切换为 textarea 编辑 markdown 原文
4. **Save 按钮** → 将编辑后的 markdown 保存：
   - 更新 `md/` 下的 markdown 文件
   - 更新对应的 JSON 数据
   - **注意**：纯静态 HTML 无法直接写文件，有两种策略：
     - **策略 A（推荐）**：提供"导出"功能，将编辑内容复制到剪贴板或生成下载文件
     - **策略 B**：配合一个小型 Python HTTP 服务器处理保存请求
5. **Step 2 子系统卡片** → 点击可展开详情，编辑子系统属性
6. **Step 3 子系统列表** → 点击子系统名称进入该 spec 的查看/编辑视图

#### 文件保存策略

推荐采用**策略 B（本地服务器模式）**：

```bash
# 生成静态文件
python3 porto_viewer.py generate --workflow {WORKFLOW_ID}

# 启动本地查看器（含保存功能的迷你 HTTP 服务器）
python3 porto_viewer.py serve --workflow {WORKFLOW_ID} --port 8080
```

服务器仅提供以下 API：
- `GET /` → 返回 `index.html`
- `GET /data/*` → 返回 JSON 静态文件
- `GET /md/*` → 返回 Markdown 文件
- `POST /api/save` → 保存编辑后的 markdown 文件到 `md/` 目录

### 3.5 Python 脚本设计

**新文件**：`skills/scripts/porto_viewer.py`

#### 子命令

| 命令 | 说明 |
|------|------|
| `generate --workflow <ID>` | 解析 workflow 产出，生成 `data/` JSON 和 `index.html` |
| `serve --workflow <ID> [--port 8080]` | 启动本地服务器查看 workflow |
| `update --workflow <ID> --step <N>` | 单步更新（某步完成后增量生成） |

#### generate 核心逻辑

```python
def generate(workflow_id):
    wf_dir = ~/.porto/workflows/{workflow_id}/
    
    # 1. 创建 data/ 目录（md/ 已由各步骤产出时创建）
    (wf_dir / "data").mkdir(exist_ok=True)
    
    # 2. 读取和解析 workflow.json
    workflow = read_json(wf_dir / "workflow.json")
    
    # 3. 处理 Step 1（md 文件已由 skill 直接写入 md/）
    step1_md = wf_dir / "md" / "step1_understanding.md"
    if step1_md.exists():
        step1_json = parse_step1(step1_md.read_text(), workflow)
        write_json(wf_dir / "data" / "step1.json", step1_json)
    
    # 4. 处理 Step 2
    step2_md = wf_dir / "md" / "step2_subsystems.md"
    if step2_md.exists():
        step2_json = parse_step2(step2_md.read_text(), workflow)
        write_json(wf_dir / "data" / "step2.json", step2_json)
    
    # 5. 处理 Step 3 (每个子系统的 spec，已由 skill 写入 md/step3/)
    step3_dir = wf_dir / "md" / "step3"
    if step3_dir.exists():
        step3_json = parse_step3(step3_dir, workflow)
        write_json(wf_dir / "data" / "step3.json", step3_json)
    
    # 6. 生成 index.html（内联 CSS/JS 的单文件）
    write(wf_dir / "index.html", generate_html())
```

#### Markdown 解析策略

使用 Python 内置能力（正则表达式）解析 markdown，提取：
- **Step 1**：按 `## ` 标题分割章节，提取 preview
- **Step 2**：解析表格和列表，提取子系统信息
- **Step 3**：遍历 `md/step3/` 子目录，提取每个 REQUIREMENTS.md 的章节结构

### 3.6 新增 Porto 子命令

在 SKILL.md 中新增 `view` 子命令：

```
/porto view [workflow_id]        # 生成并打开查看器
/porto view --serve [--port N]   # 启动带保存功能的本地服务器
```

需要新增的文件：
- `skills/en/references/view.md` — view 命令实现
- `skills/zhcn/references/view.md` — view 命令实现（中文版）

### 3.7 自动生成时机

在每步完成时（`step-complete`），自动调用 `porto_viewer.py update` 增量更新 viewer 数据：

修改 `porto_workflow.py` 的 `cmd_step_complete`，在步骤完成后自动触发 viewer 更新。

或者在 `continue.md` skill 的步骤完成逻辑中，新增调用：

```bash
python3 {skills_scripts_dir}/porto_viewer.py update --workflow "{WORKFLOW_ID}" --step {N}
```

---

## 四、实施顺序

### Phase 1：知识库泛化（优先）

预估涉及 ~18 个文件的修改，建议按以下顺序：

1. **修改配置文件** `config.example.json`
2. **重写 knowledge-retrieval.md**（EN + ZHCN）— 核心检索逻辑
3. **更新 SKILL.md**（EN + ZHCN）— 入口描述和资源表
4. **更新 subsystem-identification.md**（EN + ZHCN）
5. **重写 subsystem-context-generation.md**（EN + ZHCN）— SCV 耦合最重
6. **更新 subsystem-specification.md**（EN + ZHCN）
7. **更新 continue.md**（EN + ZHCN）— 如有引用
8. **更新文档** README.md、docs/README_zhcn.md、CLAUDE.md
9. **更新 agents**（EN + ZHCN）

### Phase 2：HTML 工作流查看器

1. **实现 `porto_viewer.py`** — generate / serve / update 子命令
2. **生成 `index.html` 模板** — 内联 CSS/JS 的单文件 SPA
3. **新增 `view.md` skill**（EN + ZHCN）
4. **更新 SKILL.md** — 添加 view 子命令路由
5. **更新 install.sh** — 确保 scripts 目录被正确安装
6. **更新文档** — README 新增 view 命令章节
7. **集成到 workflow** — step-complete 时自动触发 viewer 更新

### Phase 3：测试验证

1. 创建一个示例 workflow 目录，手动放入各步骤的 markdown 产出
2. 运行 `porto_viewer.py generate` 验证 JSON 解析正确
3. 运行 `porto_viewer.py serve` 验证 HTML 渲染和编辑保存功能
4. 验证知识库配置多个源时的检索逻辑
5. 确保安装脚本正常工作

---

## 五、注意事项与约束

### 5.1 已知问题

- **步骤编号不一致**：当前代码库中存在 3 步 vs 4 步的不一致：
  - `config.example.json` 定义了 3 步（Step 3 = spec generation，输出到 `step3/`）
  - `SKILL.md`、`README.md`、`porto_workflow.py` 定义了 4 步（Step 3 = context generation，Step 4 = spec generation）
  - 本计划以 `config.example.json` 的 3 步为准（与最新代码一致）
  - 如需保留 4 步，需额外同步

### 5.2 设计约束

- HTML 查看器必须是**纯静态单文件**（`index.html`），无需 npm/node 构建
- Python 脚本使用**标准库**，不引入第三方依赖（除 markdown 解析可用 `re`）
- JS 外部依赖仅通过 **CDN** 引入（marked.js、mermaid.js），并提供 fallback
- 编辑保存功能需要启动本地服务器（`serve` 模式），纯 `generate` 模式仅支持只读浏览

### 5.3 双语同步

每次修改 `skills/en/` 下的文件，都必须同步修改 `skills/zhcn/` 下的对应文件。建议逐文件对照修改，而非批量处理。

---

## 六、附录

### A. SCV 引用全量扫描结果

以下是当前代码库中所有 SCV 相关引用的位置（grep 扫描）：

| 文件 | 引用次数 | 关键模式 |
|------|----------|----------|
| `skills/en/references/subsystem-context-generation.md` | ~30 | `~/.scv/repos/`, SCV 搜索命令 |
| `skills/zhcn/references/subsystem-context-generation.md` | ~30 | 同上 |
| `skills/en/references/knowledge-retrieval.md` | ~15 | `~/.scv/analysis/` 检索逻辑 |
| `skills/zhcn/references/knowledge-retrieval.md` | ~15 | 同上 |
| `skills/en/references/subsystem-specification.md` | ~10 | `~/.scv/analysis/` 模式引用 |
| `skills/zhcn/references/subsystem-specification.md` | ~10 | 同上 |
| `skills/en/SKILL.md` | ~4 | 资源表 |
| `skills/zhcn/SKILL.md` | ~4 | 资源表 |
| `README.md` | ~12 | 知识库集成章节 |
| `docs/README_zhcn.md` | ~15 | 知识库集成章节 |
| `config.example.json` | 2 | `scv_knowledge_base`, `scv_templates` |
| `CLAUDE.md` | ~3 | SCV 依赖描述 |

### B. 外部 CDN 依赖

```html
<!-- Marked.js - Markdown → HTML -->
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>

<!-- Mermaid.js - 图表渲染 -->
<script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js"></script>
```
