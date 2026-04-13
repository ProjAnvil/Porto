---
name: knowledge-retrieval
description: |
  从配置的知识库中检索和搜索架构知识。
  当需要引用现有系统设计、查找类似微服务模式或检查与当前系统的潜在冲突时使用此 skill。
  当用户提到"知识库"、"现有系统"、"参考"、"类似系统"或需要了解当前架构概况时也使用此 skill。
allowed-tools: Read, Glob, Grep, Bash
---

# 知识检索

## 指令

此 skill 从 `~/.porto/config.json` 中配置的知识库检索架构知识。Porto 支持多个知识库源——每个都是包含分析文档、代码或架构参考的目录。

### 1. 加载知识库配置

```bash
cat ~/.porto/config.json | python3 -c "
import json, sys
config = json.load(sys.stdin)
for kb in config.get('knowledge_bases', []):
    if kb.get('enabled', True):
        print(f\"{kb['name']} ({kb['type']}): {kb['path']}\")
"
```

如果没有配置知识库或没有启用的知识库，跳过检索并提示：
```
⚠️ 未配置知识库。不使用知识库引用继续执行。
```

### 2. 检索策略

对每个启用的 `directory` 类型知识库：

#### 策略 1: 精确匹配（优先）

当 PRD 明确提及与现有系统集成时：

1. 搜索 `{kb_path}/{system_name}/` 目录
2. 如果找到，读取分析文档：`README.md`、`SUMMARY.md`、`ARCHITECTURE.md`、`FILE_INDEX.md`
3. 提取：API、数据模型、架构模式、依赖

#### 策略 2: 相似性匹配（备选）

当未找到精确匹配时：

1. 在知识库的所有子目录中搜索相关关键词
2. 按以下维度匹配：
   - 业务领域（如"用户管理"、"订单处理"、"通知"）
   - 技术栈（如"Go"、"Java Spring"、"Python FastAPI"）
   - 架构模式（如"事件驱动"、"CQRS"、"微服务"）

#### 策略 3: 混合（推荐）

结合两种策略：
1. 首先在所有知识库中尝试精确匹配
2. 如果无匹配或部分匹配，用相似性搜索补充
3. 返回最相关的前 N 个引用（N 来自配置 `knowledge_retrieval.max_references`）

### 3. 搜索命令

```bash
# 列出知识库内容
ls -la {kb_path}/

# 按关键词搜索知识库中的系统
grep -r "关键词" {kb_path}/*/SUMMARY.md 2>/dev/null
grep -r "关键词" {kb_path}/*/README.md 2>/dev/null

# 搜索特定模式
find {kb_path} -name "*.md" | xargs grep -l "关键词" 2>/dev/null
```

如果知识库配置了 `repos_path`（如代码仓库）：
```bash
# 搜索代码仓库
ls {kb_repos_path}/
grep -r "pattern" {kb_repos_path}/{subsystem}/ 2>/dev/null
```

### 4. 知识库类型

| 类型 | 说明 | 搜索方式 |
|------|------|----------|
| `directory` | 包含分析文档/代码的本地目录 | 文件系统搜索（grep、find、ls） |
| `db` | 数据库存储的知识库 | ⚠️ 暂不支持 |

## 输出格式

检索知识时，按以下结构输出：

```markdown
## 知识库引用

### 已查询的来源

| 知识库 | 类型 | 路径 | 找到系统数 |
|--------|------|------|-----------|
| scv | directory | ~/.scv/analysis | 2 |
| internal-docs | directory | ~/company/docs | 1 |

### 匹配的系统

| 系统 | 匹配类型 | 相关度 | 来源知识库 |
|------|----------|--------|-----------|
| imed-process | 精确 | 100% | scv |
| ircs-notice | 相似 | 75% | scv |

### 引用详情

#### imed-process

**来源**: scv → ~/.scv/analysis/imed-process/

**关键架构模式**:
- 事件驱动通信
- 订单处理使用 CQRS
- PostgreSQL + Redis 缓存

**相关 API**:
| 方法 | 端点 | 描述 |
|------|------|------|
| POST | /api/v1/orders | 创建订单 |

**数据模型**:
- Order（核心实体）
- OrderItem（订单项）
- OrderStatus（状态机）

**集成点**:
- 发布: OrderCreated, OrderCompleted
- 消费: PaymentProcessed, InventoryReserved
```

### 建议

基于知识库分析：

1. **复用**: 采用 imed-process 的事件模式以保持一致性
2. **扩展**: ircs-notice 的通知模式可以适配
3. **新建**: 用户认证需要新实现
```

## 配置

检索行为可在 `~/.porto/config.json` 中配置：

```json
{
  "knowledge_retrieval": {
    "enabled": true,
    "match_strategy": "hybrid",
    "similarity_threshold": 0.6,
    "max_references": 5
  }
}
```

## 降级行为

如果没有配置知识库或所有配置的路径为空：

1. 记录警告消息
2. 在没有知识库引用的情况下继续
3. 使用通用最佳实践进行架构设计
4. 建议在 `~/.porto/config.json` 中配置知识库
