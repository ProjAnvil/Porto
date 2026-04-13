# Porto - 业务需求分解系统

> 将业务需求转换为可执行的子系统规格说明。

[English](../README.md) | **中文**

Porto 是一个 AI 原生系统，将产品需求文档（PRD）分解为详细的子系统级需求规格说明。

## 核心概念

Porto 通过 **4 步工作流** 转换业务需求：

1. **理解** - 分析 PRD 提取业务需求
2. **识别** - 基于业务能力定义子系统边界
3. **上下文化** - 从知识库代码分析生成交互图
4. **规格化** - 为每个子系统生成详细需求规格

```
PRD 文档 → 业务理解 → 子系统识别 → 上下文图 → 子系统规格
                                              ↓
                                  step4/{subsystem}/REQUIREMENTS.md
```

## 快速开始

```bash
# 1. 安装 Porto
./install.sh --lang=zh-cn

# 2. （可选）在 ~/.porto/config.json 中配置知识库

# 3. 启动新工作流
/porto.gen docs/requirements.md --name "我的项目"

# 4. 查看输出，然后继续
/porto.continue

# 5. 重复直到完成（4 个步骤）
```

---

## 安装

```bash
# 运行安装脚本（中文版）
cd /path/to/Porto
./install.sh --lang=zh-cn

# 或安装英文版
./install.sh --lang=en
```

**语言选项**：
| 选项 | 描述 |
|------|------|
| `--lang=zh-cn` | 中文版 |
| `--lang=en` | 英文版（默认） |

---

## 可用命令

| 命令 | 描述 |
|------|------|
| `/porto.gen` | 启动新的 PRD 分解工作流 |
| `/porto.continue` | 继续到下一步 |
| `/porto.resume` | 恢复中断的工作流 |
| `/porto.status` | 查看详细工作流状态 |
| `/porto.list` | 列出所有工作流（支持过滤） |

---

## 工作流概览

Porto 使用 **4 步交互式工作流**：

### Step 1: 业务理解

**输入**：PRD 文档
**输出**：`step1_understanding.md`

分析并提取：
- 业务目标和目标用户
- 核心业务流程
- 功能分解（P0/P1/P2 优先级）
- 领域实体和关系
- 非功能性需求
- **子系统线索**（供 Step 2 使用）

### Step 2: 子系统识别

**输入**：`step1_understanding.md` + 知识库
**输出**：`step2_subsystems.md`

识别并定义：
- 子系统边界（基于 DDD 限界上下文）
- 子系统职责
- 子系统间依赖关系
- 初步 API 定义
- 技术栈建议
- **引用知识库中的现有系统**

### Step 3: 上下文生成

**输入**：`step1_understanding.md` + `step2_subsystems.md` + 知识库仓库
**输出**：`step3_context.md`

分析知识库中的代码仓库并生成：
- **时序图**（业务流程）
- **状态图**（实体状态机）
- **流程图**（决策逻辑）
- **组件图**（系统架构）
- **ER 图**（数据模型）
- **事件目录**（发布/消费的事件）
- **集成契约**（API、异步事件）

### Step 4: 子系统规格

**输入**：`step1_understanding.md` + `step2_subsystems.md` + `step3_context.md`
**输出**：`step4/{subsystem_name}/REQUIREMENTS.md`（每个子系统）

为每个子系统生成详细规格：
- 业务能力和验收标准
- API 契约（端点、请求/响应模式）
- 数据模型需求（实体、关系、索引）
- 集成需求（依赖、事件）
- 非功能性需求（性能、安全、可扩展性）
- 技术建议及理由

---

## 输出结构

```
~/.porto/
├── config.json                    # Porto 配置
└── workflows/
    └── {workflow_id}/
        ├── workflow.json          # 工作流元数据
        ├── current_step           # 当前步骤 (1/2/3/4)
        ├── inputs/                # 原始 PRD 文件
        │   └── requirements.md
        ├── step1_understanding.md # 业务需求
        ├── step2_subsystems.md    # 子系统定义
        ├── step3_context.md       # 交互图
        └── step4/                 # 每个子系统规格
            ├── imed-process/
            │   └── REQUIREMENTS.md
            ├── ircs-notice/
            │   └── REQUIREMENTS.md
            └── payment-gateway/
                └── REQUIREMENTS.md
```

### Step 3 上下文输出示例

`step3_context.md` 包含 Mermaid 图表：

```markdown
## 订单创建流程

\`\`\`mermaid
sequenceDiagram
    participant Client as 客户端
    participant Gateway as API 网关
    participant Order as imed-process
    participant Inventory as 库存服务
    participant Payment as 支付网关
    participant MQ as 消息队列
    participant Notify as ircs-notice

    Client->>Gateway: POST /api/v1/orders
    Gateway->>Order: CreateOrder(request)
    Order->>Inventory: CheckStock(items)
    Inventory-->>Order: 库存充足
    Order->>Order: 创建订单实体
    Order->>MQ: 发布 OrderCreated
    Order-->>Gateway: 订单已创建 (201)

    MQ->>Payment: 处理支付
    Payment->>MQ: 发布 PaymentCompleted
    MQ->>Notify: 发送确认通知
\`\`\`

## 订单状态机

\`\`\`mermaid
stateDiagram-v2
    [*] --> Created: 下单
    Created --> Validated: 库存确认
    Validated --> Paid: 支付成功
    Paid --> Shipped: 商品发货
    Shipped --> Delivered: 确认收货
    Delivered --> [*]
\`\`\`
```

### Step 4 规格示例

`step4/imed-process/REQUIREMENTS.md`：

```markdown
# imed-process - 系统需求

## 1. 执行摘要

| 属性 | 值 |
|------|-----|
| **名称** | imed-process |
| **类型** | extend |
| **职责** | 订单处理和管理 |
| **负责人** | 订单团队 |

## 2. 业务能力

| ID | 能力 | 优先级 |
|----|------|--------|
| BC-001 | 订单创建 | P0 |
| BC-002 | 订单追踪 | P0 |
| BC-003 | 订单取消 | P1 |

### BC-001: 订单创建

**验收标准**：
- [ ] 支持单个和批量订单创建
- [ ] 订单确认前验证库存
- [ ] 生成唯一订单号

## 3. API 需求

| 方法 | 端点 | 描述 |
|------|------|------|
| POST | /api/v1/orders | 创建订单 |
| GET | /api/v1/orders/{id} | 获取订单详情 |
| PUT | /api/v1/orders/{id}/status | 更新订单状态 |

## 4. 数据模型需求

| 属性 | 类型 | 必需 | 描述 |
|------|------|------|------|
| id | string | 是 | UUID |
| customerId | string | 是 | 客户引用 |
| status | enum | 是 | created/paid/shipped/delivered |
| totalAmount | decimal | 是 | 订单总额 |

## 5. 非功能性需求

| 指标 | 需求 |
|------|------|
| 响应时间 | < 200ms (P95) |
| 可用性 | 99.9% |
| 吞吐量 | 1000 req/sec |

**参考**: 知识库分析文档
```

---

## 知识库集成

Porto 引用配置的知识库用于：

- **Step 2**：识别现有系统是否能满足需求
- **Step 3**：从仓库源码分析实际代码模式
- **Step 4**：提取模式和约定用于规格生成

### 配置

知识库在 `~/.porto/config.json` 中配置：

```json
{
  "knowledge_bases": [
    {
      "name": "my-kb",
      "type": "directory",
      "path": "/path/to/analysis",
      "repos_path": "/path/to/repos",
      "description": "现有系统分析",
      "enabled": true
    }
  ]
}
```

### 支持的类型

| 类型 | 描述 | 状态 |
|------|------|------|
| `directory` | 本地目录分析文件 | 已支持 |
| `db` | 数据库知识存储 | 计划中 |

---

## 命令详情

### `/porto.gen` - 启动工作流

```bash
/porto.gen docs/requirements.md
/porto.gen docs/backend.md docs/frontend.md --name "电商平台"
```

### `/porto.continue` - 下一步

```bash
/porto.continue
```

### `/porto.resume` - 恢复工作流

```bash
/porto.resume a7f3c8b1
```

### `/porto.status` - 查看状态

```bash
/porto.status
/porto.status a7f3c8b1
/porto.status --full
```

### `/porto.list` - 列出工作流

```bash
/porto.list
/porto.list --all
/porto.list --status in_progress
/porto.list --detail
```

---

## 项目结构

```
Porto/
├── install.sh
├── config.example.json
├── README.md                           # 英文文档
├── docs/
│   └── README_zhcn.md                  # 中文文档
├── skills/                             # 执行能力
│   ├── en/                             # 英文版
│   │   ├── prd-decomposition.md        # Step 1: 理解
│   │   ├── subsystem-identification.md # Step 2: 识别
│   │   ├── subsystem-context-generation.md # Step 3: 上下文
│   │   ├── subsystem-specification.md  # Step 4: 规格
    │   └── knowledge-retrieval.md      # 知识库访问
│   └── zhcn/                           # 中文版
│       ├── prd-decomposition.md
│       ├── subsystem-identification.md
│       ├── subsystem-context-generation.md
│       ├── subsystem-specification.md
│       └── knowledge-retrieval.md
└── commands/                           # 用户入口
    ├── en/                             # 英文版
    │   ├── porto.gen.md
    │   ├── porto.continue.md
    │   ├── porto.resume.md
    │   ├── porto.status.md
    │   └── porto.list.md
    └── zhcn/                           # 中文版
        ├── porto.gen.md
        ├── porto.continue.md
        ├── porto.resume.md
        ├── porto.status.md
        └── porto.list.md
```

---

## 最佳实践

1. **审查每步输出**：在继续下一步之前编辑输出
2. **构建知识库**：配置知识库以获得更好的模式参考
3. **使用清晰的子系统名称**：帮助匹配现有系统
4. **迭代**：随着需求演进重新运行工作流

---

## 知识库关系

| 特性 | 知识库 | Porto |
|------|--------|-------|
| **用途** | 存储现有代码库分析 | 分解新需求 |
| **输入** | Git 仓库 | PRD 文档 |
| **输出** | 分析文档 | 子系统需求规格 |
| **工作流** | 单次运行 | 交互式 4 步 |
| **关系** | 生成知识库 | 消费知识库获取模式 |

---

## 许可证

MIT License
