# payment-core — 系统需求规格

## 1. 执行摘要

| 属性 | 值 |
|------|-----|
| **名称** | payment-core |
| **类型** | new |
| **职责** | 统一收单网关，管理支付订单全生命周期（创建、查询、关闭、退款），内含渠道路由与降级策略 |
| **负责人** | 支付平台团队 |
| **预计规模** | ~15 API, ~6 核心实体 |

## 2. 业务能力

| ID | 能力 | 优先级 | 验收标准 |
|----|------|--------|----------|
| BC-001 | 统一下单 | P0 | 支持银行卡/微信/支付宝/Apple Pay 四种渠道，幂等创建 |
| BC-002 | 订单查询 | P0 | 支持按订单号、商户订单号查询，响应 < 50ms |
| BC-003 | 订单关闭 | P0 | 未支付订单可主动关闭，关闭后渠道侧同步撤销 |
| BC-004 | 渠道路由 | P0 | 基于成功率、费率、限额的智能路由，支持权重和优先级 |
| BC-005 | 渠道降级 | P0 | 渠道故障自动熔断，切换备用通道，恢复后自动探活 |
| BC-006 | 退款处理 | P0 | 支持全额/部分退款，退款金额不超过原订单 |

### BC-001: 统一下单

**详细验收标准**：
- [ ] 商户订单号全局唯一，重复请求返回已有订单（幂等）
- [ ] 金额精度支持到分（0.01），最大单笔 100 万元
- [ ] 支持 CNY 币种（后续扩展多币种）
- [ ] 请求参数签名校验（RSA2 / HMAC-SHA256）
- [ ] 超时未支付订单自动关闭（默认 30 分钟，商户可配置）

### BC-004: 渠道路由

**路由策略优先级**：
1. 商户指定渠道（强制路由）
2. 智能路由：成功率权重 40% + 费率权重 30% + 响应速度权重 30%
3. 降级路由：主渠道熔断后切换备用渠道

## 3. API 需求

| Method | Endpoint | 描述 | 鉴权 |
|--------|----------|------|------|
| POST | `/api/v1/payments` | 统一下单 | 商户签名 |
| GET | `/api/v1/payments/{paymentId}` | 按平台订单号查询 | 商户签名 |
| GET | `/api/v1/payments/merchant/{merchantOrderNo}` | 按商户订单号查询 | 商户签名 |
| POST | `/api/v1/payments/{paymentId}/close` | 关闭订单 | 商户签名 |
| POST | `/api/v1/refunds` | 发起退款 | 商户签名 |
| GET | `/api/v1/refunds/{refundId}` | 退款查询 | 商户签名 |
| POST | `/internal/payments/callback` | 渠道异步回调 | 渠道签名校验 |

### POST /api/v1/payments — 统一下单

**Request**:
```json
{
  "merchantOrderNo": "M2026041000001",
  "amount": 9900,
  "currency": "CNY",
  "channelType": "WECHAT_PAY",
  "subject": "订单支付",
  "description": "商品购买 x2",
  "clientIp": "123.45.67.89",
  "expireTime": "2026-04-10T10:30:00+08:00",
  "notifyUrl": "https://merchant.com/webhook/payment",
  "metadata": {"orderId": "BIZ-001"}
}
```

**Response (201)**:
```json
{
  "paymentId": "PAY202604100000001",
  "merchantOrderNo": "M2026041000001",
  "status": "PAYING",
  "amount": 9900,
  "currency": "CNY",
  "channelType": "WECHAT_PAY",
  "payUrl": "weixin://wxpay/bizpayurl?...",
  "expireTime": "2026-04-10T10:30:00+08:00",
  "createdAt": "2026-04-10T10:00:00+08:00"
}
```

## 4. 数据模型需求

### PaymentOrder

| 字段 | 类型 | 必填 | 描述 | 索引 |
|------|------|------|------|------|
| order_id | VARCHAR(32) | ✓ | 平台订单号 | PK |
| merchant_id | VARCHAR(32) | ✓ | 商户ID | IDX |
| merchant_order_no | VARCHAR(64) | ✓ | 商户订单号 | UNI(merchant_id, merchant_order_no) |
| amount | BIGINT | ✓ | 金额（分） | — |
| currency | VARCHAR(3) | ✓ | 币种 | — |
| status | VARCHAR(20) | ✓ | 订单状态 | IDX |
| channel_id | VARCHAR(32) | — | 渠道ID | IDX |
| channel_type | VARCHAR(20) | ✓ | 渠道类型 | — |
| channel_order_no | VARCHAR(64) | — | 渠道订单号 | IDX |
| risk_level | VARCHAR(10) | — | 风控等级 | — |
| subject | VARCHAR(128) | ✓ | 订单标题 | — |
| client_ip | VARCHAR(45) | — | 客户端IP | — |
| notify_url | VARCHAR(256) | ✓ | 通知地址 | — |
| expire_time | TIMESTAMP | ✓ | 过期时间 | — |
| paid_at | TIMESTAMP | — | 支付成功时间 | — |
| closed_at | TIMESTAMP | — | 关闭时间 | — |
| refunded_amount | BIGINT | ✓ | 已退款金额（分） | — |
| metadata | JSONB | — | 商户扩展字段 | — |
| created_at | TIMESTAMP | ✓ | 创建时间 | IDX |
| updated_at | TIMESTAMP | ✓ | 更新时间 | — |

### RefundOrder

| 字段 | 类型 | 必填 | 描述 |
|------|------|------|------|
| refund_id | VARCHAR(32) | ✓ | 退款单号 (PK) |
| original_order_id | VARCHAR(32) | ✓ | 原订单号 (FK → PaymentOrder) |
| merchant_refund_no | VARCHAR(64) | ✓ | 商户退款号 |
| refund_amount | BIGINT | ✓ | 退款金额（分） |
| reason | VARCHAR(256) | — | 退款原因 |
| status | VARCHAR(20) | ✓ | 退款状态 |
| channel_refund_no | VARCHAR(64) | — | 渠道退款号 |
| created_at | TIMESTAMP | ✓ | 创建时间 |
| completed_at | TIMESTAMP | — | 完成时间 |

### Channel

| 字段 | 类型 | 必填 | 描述 |
|------|------|------|------|
| channel_id | VARCHAR(32) | ✓ | 渠道ID (PK) |
| channel_type | VARCHAR(20) | ✓ | 渠道类型 |
| name | VARCHAR(64) | ✓ | 渠道名称 |
| status | VARCHAR(10) | ✓ | 启用/停用/熔断 |
| fee_rate | DECIMAL(6,4) | ✓ | 费率 |
| success_rate | DECIMAL(5,2) | — | 近期成功率 |
| daily_limit | BIGINT | — | 日限额（分） |
| single_limit | BIGINT | — | 单笔限额（分） |
| circuit_breaker_config | JSONB | — | 熔断配置 |

## 5. 集成需求

| 依赖方 | 协议 | 接口 | 用途 | 容错策略 |
|--------|------|------|------|----------|
| risk-engine | gRPC | `RiskEvaluate` | 实时风控评估 | 超时50ms降级放行(低风险) |
| notification-service | Kafka | `payment.notify` topic | 支付/退款结果通知 | 异步，MQ 保障 |
| 微信支付 | HTTPS | 统一下单/查询/退款 | 渠道扣款 | 熔断 + 备用渠道 |
| 支付宝 | HTTPS | 当面付/手机网站支付 | 渠道扣款 | 熔断 + 备用渠道 |
| 银联 | HTTPS | 在线网关支付 | 银行卡渠道 | 熔断 + 备用渠道 |

## 6. 非功能性需求

| 指标 | 需求 | 依据 |
|------|------|------|
| P95 响应时间 | < 200ms | BO-01 业务目标 |
| P99 响应时间 | < 500ms | 支付体验要求 |
| 可用性 | 99.99% (年停机 < 52 分钟) | 金融系统标准 |
| 峰值 TPS | 3,000 | PRD 明确要求 |
| 数据一致性 | 强一致（TCC 事务） | 资金安全 |
| 幂等性 | 商户订单号 + 商户ID 联合去重 | 防止重复扣款 |
| 日志审计 | 全量交易日志，保留 3 年 | 合规要求 |

## 7. 技术建议

- **订单号生成**：Snowflake 变体（机房+机器+时间+序列），保证全局唯一且有序
- **幂等处理**：Redis SET NX + 数据库唯一约束双重保障
- **渠道适配器模式**：Strategy Pattern，每个渠道一个 Adapter 实现，统一接口
- **熔断降级**：Resilience4j CircuitBreaker，阈值 50% 失败率触发，半开探活
- **分库分表**：按 merchant_id 分片，预估 16 库 x 64 表
