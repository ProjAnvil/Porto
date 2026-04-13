# risk-engine — 系统需求规格

## 1. 执行摘要

| 属性 | 值 |
|------|-----|
| **名称** | risk-engine |
| **类型** | new |
| **职责** | 实时风控决策引擎，基于规则和 ML 模型评估交易风险等级，管理风控规则和黑白名单 |
| **负责人** | 风控团队 |
| **预计规模** | ~8 API, ~4 核心实体 |

## 2. 业务能力

| ID | 能力 | 优先级 | 验收标准 |
|----|------|--------|----------|
| BC-001 | 实时风控评估 | P0 | 决策延迟 < 50ms (P99)，拦截率 ≥ 99.5% |
| BC-002 | 规则管理 | P1 | 规则热更新，无需重启服务 |
| BC-003 | 黑白名单管理 | P1 | 支持实时生效，百万级名单毫秒级查询 |

### BC-001: 实时风控评估

**评估维度**：

| 维度 | 特征 | 权重 |
|------|------|------|
| 用户画像 | 注册天数、历史交易次数、历史拒绝次数 | 20% |
| 交易特征 | 金额、频率、时间段、IP 地理位置 | 30% |
| 设备指纹 | 设备ID、UA、屏幕分辨率、是否模拟器 | 25% |
| 关联网络 | 同设备关联账户数、同IP关联交易数 | 25% |

**决策结果**：

| 风险等级 | 分数区间 | 动作 |
|----------|----------|------|
| LOW | 0-30 | 放行 |
| MEDIUM | 31-60 | 放行 + 监控 |
| HIGH | 61-85 | 人工审核 |
| CRITICAL | 86-100 | 直接拒绝 |

## 3. API 需求

| Method | Endpoint | 描述 | 协议 |
|--------|----------|------|------|
| POST | `/risk/evaluate` | 实时风控评估 | gRPC |
| GET | `/api/v1/risk/rules` | 查询规则列表 | REST |
| POST | `/api/v1/risk/rules` | 创建风控规则 | REST |
| PUT | `/api/v1/risk/rules/{id}` | 更新风控规则 | REST |
| DELETE | `/api/v1/risk/rules/{id}` | 删除风控规则 | REST |
| POST | `/api/v1/risk/blacklist` | 添加黑名单 | REST |
| DELETE | `/api/v1/risk/blacklist/{entry}` | 移除黑名单 | REST |
| GET | `/api/v1/risk/events` | 查询风控事件 | REST |

### gRPC: RiskEvaluate

**Request**:
```protobuf
message RiskRequest {
  string order_id = 1;
  string merchant_id = 2;
  int64 amount = 3;
  string currency = 4;
  string user_id = 5;
  string client_ip = 6;
  string device_id = 7;
  string user_agent = 8;
  map<string, string> extra_features = 9;
}
```

**Response**:
```protobuf
message RiskResponse {
  string event_id = 1;
  RiskLevel risk_level = 2;
  int32 risk_score = 3;
  string action = 4;  // PASS, REVIEW, REJECT
  repeated RuleHit rule_hits = 5;
  int64 latency_ms = 6;
}
```

## 4. 数据模型需求

### RiskRule

| 字段 | 类型 | 必填 | 描述 |
|------|------|------|------|
| rule_id | VARCHAR(32) | ✓ | 规则ID (PK) |
| name | VARCHAR(128) | ✓ | 规则名称 |
| category | VARCHAR(32) | ✓ | 规则分类（velocity/amount/blacklist/ml） |
| expression | TEXT | ✓ | 规则表达式 (Drools DRL / MVEL) |
| risk_score | INT | ✓ | 触发后增加的风险分数 |
| action | VARCHAR(20) | ✓ | 触发动作 |
| priority | INT | ✓ | 优先级（越小越先执行） |
| status | VARCHAR(10) | ✓ | enabled/disabled |
| version | INT | ✓ | 版本号（乐观锁） |
| created_at | TIMESTAMP | ✓ | 创建时间 |

### RiskEvent

| 字段 | 类型 | 必填 | 描述 |
|------|------|------|------|
| event_id | VARCHAR(32) | ✓ | 事件ID (PK) |
| order_id | VARCHAR(32) | ✓ | 关联订单号 |
| merchant_id | VARCHAR(32) | ✓ | 商户ID |
| risk_level | VARCHAR(10) | ✓ | 风险等级 |
| risk_score | INT | ✓ | 风险评分 |
| action | VARCHAR(20) | ✓ | 决策动作 |
| rule_hits | JSONB | — | 命中规则详情 |
| features | JSONB | — | 特征快照 |
| latency_ms | INT | ✓ | 决策耗时(ms) |
| created_at | TIMESTAMP | ✓ | 评估时间 |

## 5. 集成需求

| 调用方 | 协议 | 说明 | SLA |
|--------|------|------|------|
| payment-core → risk-engine | gRPC | 下单时同步调用 | < 50ms P99 |

## 6. 非功能性需求

| 指标 | 需求 |
|------|------|
| P99 决策延迟 | < 50ms |
| 可用性 | 99.95% |
| 规则热更新 | < 5 秒生效 |
| 黑名单查询 | < 1ms (Bloom Filter + Redis) |
| 事件存储 | 保留 180 天，归档至 ClickHouse |
| 日吞吐量 | 100 万次评估 |

## 7. 技术建议

- **规则引擎**：Drools 7.x，规则以 DRL 存储在数据库，KieScanner 定期刷新
- **特征存储**：Redis Hash 存储用户实时特征（滑动窗口计数器）
- **黑名单**：Bloom Filter 初筛 + Redis Set 精确匹配
- **ML 模型**：ONNX Runtime 本地推理，模型通过 S3 版本化管理
- **监控**：风控命中率、误杀率、延迟分布实时看板
