# Step 2: 子系统识别 — 互联网支付交易平台

## 识别概览

基于 Step 1 的业务需求分析，结合 DDD 限界上下文划分原则，共识别出 **5 个子系统**：

| # | 子系统 | 类型 | 核心职责 | 功能数 |
|---|--------|------|----------|--------|
| 1 | payment-core | new | 统一收单、渠道路由、订单生命周期管理 | 5 |
| 2 | risk-engine | new | 实时风控决策、规则引擎、黑白名单 | 3 |
| 3 | settlement-service | new | 交易对账、清算汇总、结算打款、分账 | 3 |
| 4 | merchant-portal | new | 商户入驻、密钥管理、交易查询、报表 | 4 |
| 5 | notification-service | extend | 支付/退款结果异步通知、重试 | 2 |

---

### payment-core

| Attribute | Value |
|-----------|-------|
| **Type** | new |
| **Responsibility** | 统一收单网关，管理支付订单全生命周期（创建、查询、关闭、退款），内含渠道路由与降级策略 |
| **Owner** | 支付平台团队 |

**核心能力**：
- 统一下单 API（F-001）
- 订单查询 API（F-002）
- 订单关闭 API（F-003）
- 渠道路由选择与降级熔断（F-004, F-005）
- 退款处理（F-009）

**关键领域实体**：PaymentOrder, RefundOrder, Channel, ChannelRoute

**依赖关系**：
- → risk-engine：下单前调用风控评估
- → notification-service：支付/退款成功后发送通知
- ← settlement-service：提供交易数据用于对账清算
- ← merchant-portal：商户通过门户查询订单

**技术核心约束**：
- P95 响应 < 200ms
- 99.99% 可用性
- 幂等性设计（商户订单号去重）
- 分布式事务（TCC / Saga）

---

### risk-engine

| Attribute | Value |
|-----------|-------|
| **Type** | new |
| **Responsibility** | 实时风控决策引擎，基于规则和 ML 模型评估交易风险，支持黑白名单管理 |
| **Owner** | 风控团队 |

**核心能力**：
- 实时风控评估（F-006）
- 风控规则管理（F-007）
- 黑白名单管理（F-008）

**关键领域实体**：RiskEvent, RiskRule

**依赖关系**：
- ← payment-core：接收交易风控评估请求
- 独立运行，无强外部依赖

**技术核心约束**：
- 决策延迟 < 50ms（P99）
- 规则热更新，无需重启
- 支持 CEP（Complex Event Processing）模式

---

### settlement-service

| Attribute | Value |
|-----------|-------|
| **Type** | new |
| **Responsibility** | 交易对账、清算汇总、结算打款和分账计算，确保资金准确性 |
| **Owner** | 清结算团队 |

**核心能力**：
- 交易对账（F-010）
- 商户结算打款（F-011）
- 分账能力（F-012）

**关键领域实体**：SettlementBatch, AccountFlow, ReconcileRecord

**依赖关系**：
- → payment-core：拉取交易数据
- → merchant-portal：获取商户费率配置
- → notification-service：结算完成通知

**技术核心约束**：
- 资金差错率 < 0.001%
- T+1 自动结算（可配置 T+0）
- 批量处理能力：50 万笔/批次
- 对账差异自动标记，人工审核兜底

---

### merchant-portal

| Attribute | Value |
|-----------|-------|
| **Type** | new |
| **Responsibility** | 商户自助管理门户，覆盖入驻、密钥管理、交易查询、报表下载 |
| **Owner** | 商户产品团队 |

**核心能力**：
- 商户入驻与审核（F-013）
- API 密钥管理（F-014）
- 交易查询与导出（F-015）
- 聚合报表（F-018）

**关键领域实体**：Merchant, MerchantKey

**依赖关系**：
- → payment-core：查询交易数据
- → settlement-service：查询结算数据
- 独立前端应用，RPC/REST 调用后端服务

**技术核心约束**：
- 商户自助完成率 ≥ 90%
- 支持 RBAC 权限模型
- 大数据量导出（异步文件生成 + 下载链接）

---

### notification-service

| Attribute | Value |
|-----------|-------|
| **Type** | extend |
| **Responsibility** | 异步消息通知服务，负责支付/退款/结算结果的 Webhook 推送与重试 |
| **Owner** | 基础平台团队 |

**核心能力**：
- 支付结果通知（F-016）
- 退款结果通知（F-017）

**关键领域实体**：NotifyTask

**依赖关系**：
- ← payment-core：接收通知任务
- ← settlement-service：接收结算完成通知
- → 商户 Webhook URL：外部推送

**技术核心约束**：
- 至少一次投递保证（at-least-once）
- 指数退避重试策略（最多 8 次，间隔 15s → 4h）
- 通知成功率 ≥ 99.9%

---

## 子系统依赖拓扑

```
merchant-portal ──→ payment-core ──→ risk-engine
       │                  │
       │                  ↓
       └──→ settlement-service ──→ notification-service
                                          ↑
                    payment-core ──────────┘
```

## 技术栈建议

| 层次 | 技术选型 | 理由 |
|------|----------|------|
| 语言 | Java 21 + Virtual Threads | 高并发、金融领域生态成熟 |
| 框架 | Spring Boot 3.x + Spring Cloud | 微服务标准方案 |
| 数据库 | PostgreSQL (OLTP) + ClickHouse (OLAP) | 事务 + 分析分离 |
| 缓存 | Redis Cluster | 渠道路由缓存、幂等去重 |
| 消息队列 | Apache Kafka | 高吞吐异步通知、对账数据流 |
| 风控引擎 | Drools + 自研特征平台 | 规则热更新 + 实时特征计算 |
| 网关 | Spring Cloud Gateway | 统一鉴权、限流、灰度 |
| 监控 | Prometheus + Grafana + Jaeger | 可观测三支柱 |
