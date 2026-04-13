# merchant-portal — 系统需求规格

## 1. 执行摘要

| 属性 | 值 |
|------|-----|
| **名称** | merchant-portal |
| **类型** | new |
| **职责** | 商户自助管理门户，提供入驻审核、密钥管理、交易查询和数据报表 |
| **负责人** | 商户平台团队 |
| **预计规模** | ~12 API, ~4 核心实体 |

## 2. 业务能力

| ID | 能力 | 优先级 | 验收标准 |
|----|------|--------|----------|
| BC-001 | 商户入驻 | P0 | 在线提交资质 → 审核 → 签约，全流程 ≤ 3 个工作日 |
| BC-002 | 密钥管理 | P0 | 支持 RSA/SM2 密钥生成、轮换、吊销 |
| BC-003 | 交易查询 | P0 | 按日期/状态/金额筛选，支持导出 CSV |
| BC-004 | 数据报表 | P1 | 交易概览、成功率趋势、渠道分布、退款分析 |
| BC-005 | 角色权限 | P1 | RBAC：管理员、财务、运营、开发者 |
| BC-006 | 操作日志 | P2 | 关键操作审计（密钥变更、退款申请、结算配置） |

### BC-001: 商户入驻流程

```
提交入驻申请
  → 上传营业执照 / 法人身份证 / 银行开户许可
  → 填写费率模板（按交易类型/渠道）
  → 运营审核（AI 预审 + 人工复核）
  → 签署电子协议
  → 分配 merchant_id + API 密钥
  → 入驻完成
```

## 3. API 需求

| Method | Endpoint | 描述 |
|--------|----------|------|
| POST | `/api/v1/merchants/register` | 提交入驻申请 |
| GET | `/api/v1/merchants/{id}` | 查询商户详情 |
| PUT | `/api/v1/merchants/{id}/status` | 审核商户（通过/驳回） |
| POST | `/api/v1/merchants/{id}/keys` | 生成新密钥对 |
| DELETE | `/api/v1/merchants/{id}/keys/{kid}` | 吊销密钥 |
| GET | `/api/v1/merchants/{id}/transactions` | 查询交易列表 |
| GET | `/api/v1/merchants/{id}/transactions/export` | 导出交易 CSV |
| GET | `/api/v1/merchants/{id}/dashboard` | 交易概览数据 |
| GET | `/api/v1/merchants/{id}/reports/trend` | 成功率趋势 |
| GET | `/api/v1/merchants/{id}/reports/channel` | 渠道分布 |
| POST | `/api/v1/auth/login` | 商户登录 |
| POST | `/api/v1/auth/mfa/verify` | MFA 验证 |

### GET /api/v1/merchants/{id}/dashboard

**Response**:
```json
{
  "date": "2024-03-15",
  "summary": {
    "total_amount": 1258000,
    "total_count": 3421,
    "success_rate": 98.7,
    "refund_count": 12,
    "refund_amount": 15600
  },
  "channel_distribution": [
    {"channel": "wechat", "count": 2100, "amount": 820000},
    {"channel": "alipay", "count": 1200, "amount": 398000},
    {"channel": "unionpay", "count": 121, "amount": 40000}
  ],
  "hourly_trend": [
    {"hour": 0, "count": 50, "amount": 18000},
    {"hour": 1, "count": 23, "amount": 8500}
  ]
}
```

## 4. 数据模型需求

### Merchant

| 字段 | 类型 | 必填 | 描述 |
|------|------|------|------|
| merchant_id | VARCHAR(32) | ✓ | 商户 ID (PK) |
| company_name | VARCHAR(128) | ✓ | 公司名称 |
| contact_name | VARCHAR(64) | ✓ | 联系人 |
| contact_phone | VARCHAR(20) | ✓ | 联系电话 |
| email | VARCHAR(128) | ✓ | 邮箱 |
| license_url | VARCHAR(512) | ✓ | 营业执照文件 |
| status | VARCHAR(20) | ✓ | PENDING/APPROVED/REJECTED/SUSPENDED |
| fee_template_id | VARCHAR(32) | — | 费率模板 |
| settlement_type | VARCHAR(10) | ✓ | T+1 / T+0 / D+1 |
| bank_account | VARCHAR(32) | ✓ | 结算银行账号 |
| bank_name | VARCHAR(64) | ✓ | 开户银行 |
| created_at | TIMESTAMP | ✓ | 创建时间 |
| approved_at | TIMESTAMP | — | 审核通过时间 |

### MerchantUser

| 字段 | 类型 | 必填 | 描述 |
|------|------|------|------|
| user_id | VARCHAR(32) | ✓ | 用户 ID (PK) |
| merchant_id | VARCHAR(32) | ✓ | 关联商户 ID (FK) |
| username | VARCHAR(64) | ✓ | 登录名 |
| password_hash | VARCHAR(128) | ✓ | 密码哈希 (bcrypt) |
| role | VARCHAR(20) | ✓ | admin/finance/operator/developer |
| mfa_enabled | BOOLEAN | ✓ | 是否启用 MFA |
| mfa_secret | VARCHAR(64) | — | TOTP 密钥（加密存储） |
| last_login_at | TIMESTAMP | — | 最后登录时间 |
| status | VARCHAR(10) | ✓ | active/disabled |

### ApiKey

| 字段 | 类型 | 必填 | 描述 |
|------|------|------|------|
| key_id | VARCHAR(32) | ✓ | 密钥 ID (PK) |
| merchant_id | VARCHAR(32) | ✓ | 关联商户 ID (FK) |
| public_key | TEXT | ✓ | RSA/SM2 公钥 |
| algorithm | VARCHAR(10) | ✓ | RSA2048/SM2 |
| status | VARCHAR(10) | ✓ | active/revoked |
| created_at | TIMESTAMP | ✓ | 创建时间 |
| expires_at | TIMESTAMP | ✓ | 过期时间 |
| revoked_at | TIMESTAMP | — | 吊销时间 |

## 5. 集成需求

| 对端 | 协议 | 说明 |
|------|------|------|
| payment-core | REST | 查询商户交易列表 |
| settlement-service | REST | 查询结算记录、配置分账规则 |
| OSS | HTTPS | 上传营业执照和资质文件 |
| notification-service | Kafka | 审核结果通知 |

## 6. 非功能性需求

| 指标 | 需求 |
|------|------|
| 页面加载 | 首屏 < 2s (LCP) |
| 登录安全 | bcrypt + TOTP MFA，连续 5 次失败锁定 15 min |
| 数据导出 | 10 万行 CSV < 10s |
| 密钥存储 | AES-256 加密存储，HSM 可选 |
| RBAC | 最小权限原则，操作级别鉴权 |
| 审计日志 | 不可篡改，保留 3 年 |

## 7. 技术建议

- **前端**：React + Ant Design Pro，路由级懒加载
- **认证**：JWT access token (15min) + refresh token (7d) + TOTP MFA
- **报表**：预计算 + 物化视图，复杂报表走 ClickHouse
- **文件上传**：直传 OSS + 回调校验，前端分片上传 > 5MB
- **密钥管理**：私钥不落库，仅存公钥；对称加密密钥托管 KMS
