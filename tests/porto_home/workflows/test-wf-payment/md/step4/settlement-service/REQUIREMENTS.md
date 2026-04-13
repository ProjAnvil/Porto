# settlement-service — 系统需求规格

## 1. 执行摘要

| 属性 | 值 |
|------|-----|
| **名称** | settlement-service |
| **类型** | new |
| **职责** | T+1 结算对账服务，管理对账、清算、资金划拨和分账 |
| **负责人** | 清结算团队 |
| **预计规模** | ~6 API, ~5 核心实体 |

## 2. 业务能力

| ID | 能力 | 优先级 | 验收标准 |
|----|------|--------|----------|
| BC-001 | 渠道对账 | P0 | 每日 T+1 凌晨自动对账，差异率 < 0.01% |
| BC-002 | 商户清算 | P0 | 按商户维度汇总清算金额，扣除手续费 |
| BC-003 | 资金划拨 | P0 | 对接银行打款接口，支持批量代付 |
| BC-004 | 分账管理 | P1 | 多方分账比例配置，担保交易确认后分账 |
| BC-005 | 差异处理 | P1 | 长款/短款自动识别，人工审核后补单/退款 |

### BC-001: 渠道对账流程

```
05:00 拉取渠道T-1对账文件（CSV/TXT）
05:15 解析对账文件，导入临时表
05:30 本地订单 LEFT JOIN 渠道订单
      → 匹配成功 → 标记已对账
      → 长款（渠道有，本地无）→ 生成差异记录
      → 短款（本地有，渠道无）→ 生成差异记录
06:00 生成对账报告，推送给运营
```

## 3. API 需求

| Method | Endpoint | 描述 |
|--------|----------|------|
| POST | `/api/v1/settlement/reconcile` | 触发手动对账 |
| GET | `/api/v1/settlement/reconcile/reports` | 查询对账报告 |
| GET | `/api/v1/settlement/clearing/{date}` | 查询清算结果 |
| POST | `/api/v1/settlement/payout` | 触发资金划拨 |
| GET | `/api/v1/settlement/payout/{batch_id}` | 查询划拨结果 |
| POST | `/api/v1/settlement/split-rules` | 配置分账规则 |

### POST /api/v1/settlement/reconcile

**Request**:
```json
{
  "channel": "wechat",
  "date": "2024-03-15",
  "file_url": "https://dl.wechat.com/reconcile/20240315.csv"
}
```

**Response**:
```json
{
  "task_id": "recon_20240315_wx",
  "status": "PROCESSING",
  "total_orders": 12580,
  "matched": 0,
  "mismatched": 0,
  "started_at": "2024-03-16T05:00:00Z"
}
```

## 4. 数据模型需求

### ReconcileTask

| 字段 | 类型 | 必填 | 描述 |
|------|------|------|------|
| task_id | VARCHAR(64) | ✓ | 对账任务 ID (PK) |
| channel | VARCHAR(20) | ✓ | 渠道编码 |
| reconcile_date | DATE | ✓ | 对账日期 |
| status | VARCHAR(20) | ✓ | PENDING/PROCESSING/COMPLETED/FAILED |
| total_orders | INT | — | 订单总数 |
| matched_count | INT | — | 匹配数 |
| long_count | INT | — | 长款数 |
| short_count | INT | — | 短款数 |
| report_url | VARCHAR(512) | — | 报告下载地址 |
| started_at | TIMESTAMP | ✓ | 开始时间 |
| completed_at | TIMESTAMP | — | 完成时间 |

### ClearingRecord

| 字段 | 类型 | 必填 | 描述 |
|------|------|------|------|
| clearing_id | VARCHAR(32) | ✓ | 清算 ID (PK) |
| merchant_id | VARCHAR(32) | ✓ | 商户 ID |
| clearing_date | DATE | ✓ | 清算日期 |
| total_amount | BIGINT | ✓ | 交易总额（分） |
| fee_amount | BIGINT | ✓ | 手续费（分） |
| net_amount | BIGINT | ✓ | 应结金额（分） |
| status | VARCHAR(20) | ✓ | PENDING/CLEARED/PAID |
| order_count | INT | ✓ | 交易笔数 |

### PayoutBatch

| 字段 | 类型 | 必填 | 描述 |
|------|------|------|------|
| batch_id | VARCHAR(32) | ✓ | 批次 ID (PK) |
| total_merchants | INT | ✓ | 打款商户数 |
| total_amount | BIGINT | ✓ | 打款总额（分） |
| success_count | INT | — | 成功笔数 |
| fail_count | INT | — | 失败笔数 |
| status | VARCHAR(20) | ✓ | CREATED/PAYING/COMPLETED/PARTIAL_FAIL |
| bank_batch_no | VARCHAR(64) | — | 银行批次号 |
| created_at | TIMESTAMP | ✓ | 创建时间 |

## 5. 集成需求

| 对端 | 协议 | 说明 |
|------|------|------|
| 微信支付对账文件 | HTTPS + SFTP | 每日凌晨拉取 T-1 对账文件 |
| 支付宝对账文件 | HTTPS | 定时下载 |
| payment-core | Kafka | 消费支付成功事件，更新本地订单快照 |
| 银行代付接口 | HTTPS + 加密 | 批量代付，异步回调 |
| notification-service | Kafka | 结算完成后通知商户 |

## 6. 非功能性需求

| 指标 | 需求 |
|------|------|
| 对账处理能力 | 50 万笔/批次 < 30 分钟 |
| 金额精度 | 以「分」为单位的 BIGINT，杜绝浮点数 |
| 幂等性 | 同一日期 + 渠道不重复对账 |
| 数据保留 | 在线 1 年，归档 5 年 |
| 可用性 | 99.9%（批处理允许 T+0 补跑） |

## 7. 技术建议

- **批处理引擎**：Spring Batch，chunk 模式处理大批量对账
- **定时调度**：XXL-JOB 分布式调度，支持失败重试和告警
- **对账算法**：基于 order_id 的 hash join，内存映射大文件
- **金额计算**：全程 long (分) 运算，输出时除以 100 转元
- **对账文件解析**：策略模式，每个渠道一个 Parser 实现
