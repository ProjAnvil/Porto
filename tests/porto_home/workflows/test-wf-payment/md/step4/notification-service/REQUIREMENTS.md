# notification-service — 系统需求规格

## 1. 执行摘要

| 属性 | 值 |
|------|-----|
| **名称** | notification-service |
| **类型** | new |
| **职责** | 异步通知服务，负责向商户发送 Webhook 回调，管理重试策略和投递状态 |
| **负责人** | 基础架构团队 |
| **预计规模** | ~5 API, ~3 核心实体 |

## 2. 业务能力

| ID | 能力 | 优先级 | 验收标准 |
|----|------|--------|----------|
| BC-001 | Webhook 投递 | P0 | 首次投递延迟 < 3s，最终投递成功率 ≥ 99.9% |
| BC-002 | 指数退避重试 | P0 | 8 次重试（15s, 30s, 3m, 10m, 30m, 1h, 2h, 4h） |
| BC-003 | 签名验证 | P0 | HMAC-SHA256 签名，防篡改和重放攻击 |
| BC-004 | 投递监控 | P1 | 失败率告警，商户级投递成功率统计 |
| BC-005 | 手动重发 | P2 | 运营后台手动触发指定通知重新投递 |

### BC-001: Webhook 投递流程

```
消费 Kafka 事件 (payment.success / refund.success / settlement.completed)
  → 组装通知报文
  → HMAC-SHA256 签名
  → POST 商户 notify_url
  → HTTP 200 + body 含 "success" → 投递成功
  → 非 200 或超时 → 进入重试队列
```

### BC-002: 重试策略

| 次数 | 延迟 | 累计时间 |
|------|------|----------|
| 1 | 15 秒 | 15s |
| 2 | 30 秒 | 45s |
| 3 | 3 分钟 | ~3.75m |
| 4 | 10 分钟 | ~13.75m |
| 5 | 30 分钟 | ~43.75m |
| 6 | 1 小时 | ~1h43m |
| 7 | 2 小时 | ~3h43m |
| 8 | 4 小时 | ~7h43m |

8 次全部失败后标记为 `DELIVERY_FAILED`，等待人工处理。

## 3. API 需求

| Method | Endpoint | 描述 |
|--------|----------|------|
| GET | `/api/v1/notifications` | 查询通知列表 |
| GET | `/api/v1/notifications/{id}` | 查询通知详情 |
| POST | `/api/v1/notifications/{id}/retry` | 手动重发通知 |
| GET | `/api/v1/notifications/stats` | 投递统计 |
| PUT | `/api/v1/notifications/config` | 更新通知配置 |

### Webhook 投递报文

**Request** (POST → 商户 notify_url):
```http
POST /payment/callback HTTP/1.1
Content-Type: application/json
X-Timestamp: 1710518400
X-Nonce: a1b2c3d4e5
X-Signature: base64(HMAC-SHA256(body + timestamp + nonce, secret))

{
  "event": "payment.success",
  "order_id": "PAY20240315001234",
  "merchant_order_no": "M20240315_001",
  "amount": 9900,
  "currency": "CNY",
  "paid_at": "2024-03-15T14:30:00+08:00",
  "channel": "wechat"
}
```

**期望响应**:
```http
HTTP/1.1 200 OK
Content-Type: text/plain

success
```

## 4. 数据模型需求

### NotificationRecord

| 字段 | 类型 | 必填 | 描述 |
|------|------|------|------|
| notification_id | VARCHAR(32) | ✓ | 通知 ID (PK) |
| event_type | VARCHAR(32) | ✓ | 事件类型 |
| order_id | VARCHAR(32) | ✓ | 关联订单号 |
| merchant_id | VARCHAR(32) | ✓ | 商户 ID |
| notify_url | VARCHAR(512) | ✓ | 回调地址 |
| payload | JSONB | ✓ | 通知报文 |
| status | VARCHAR(20) | ✓ | PENDING/DELIVERING/SUCCESS/FAILED |
| retry_count | INT | ✓ | 已重试次数 (默认 0) |
| max_retries | INT | ✓ | 最大重试次数 (默认 8) |
| next_retry_at | TIMESTAMP | — | 下次重试时间 |
| last_response_code | INT | — | 最后一次 HTTP 状态码 |
| last_response_body | VARCHAR(1024) | — | 最后一次响应体（截断） |
| created_at | TIMESTAMP | ✓ | 创建时间 |
| delivered_at | TIMESTAMP | — | 投递成功时间 |

### NotificationConfig

| 字段 | 类型 | 必填 | 描述 |
|------|------|------|------|
| config_id | VARCHAR(32) | ✓ | 配置 ID (PK) |
| merchant_id | VARCHAR(32) | ✓ | 商户 ID |
| notify_url | VARCHAR(512) | ✓ | 默认回调地址 |
| secret_key | VARCHAR(128) | ✓ | HMAC 签名密钥（加密存储） |
| events | JSONB | ✓ | 订阅事件列表 |
| status | VARCHAR(10) | ✓ | active/disabled |

## 5. 集成需求

| 对端 | 协议 | 说明 |
|------|------|------|
| payment-core → notification | Kafka | 消费支付/退款完成事件 |
| settlement-service → notification | Kafka | 消费结算完成事件 |
| notification → 商户 | HTTPS | Webhook POST 回调 |
| merchant-portal → notification | REST | 查询投递记录、手动重发 |

## 6. 非功能性需求

| 指标 | 需求 |
|------|------|
| 首次投递延迟 | < 3 秒（P95） |
| 最终成功率 | ≥ 99.9% (8 次重试后) |
| 并发投递 | 1000 TPS |
| 防重放 | timestamp + nonce 5 分钟窗口 |
| 超时设置 | 连接超时 5s，读取超时 10s |
| At-least-once | 保证至少一次投递，商户侧需幂等 |

## 7. 技术建议

- **消息消费**：Kafka Consumer Group，每个 partition 独立消费
- **重试队列**：Redis Sorted Set (score = next_retry_at)，定时器扫描投递
- **HTTP 客户端**：OkHttp + 连接池，异步非阻塞投递
- **签名算法**：`HMAC-SHA256(body + "\n" + timestamp + "\n" + nonce, secret)`
- **降级策略**：某商户连续失败率 > 50% 时，自动降频投递，触发告警
- **监控指标**：投递成功率、平均延迟、重试分布、商户级失败率 Top-N
