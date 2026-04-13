# Step 3: 上下文生成 — 互联网支付交易平台

## 系统架构组件图

```mermaid
graph TB
    subgraph External["外部系统"]
        Consumer["C端消费者<br/>App/H5/小程序"]
        MerchantSys["商户系统"]
        BankChannel["银行渠道<br/>银联/网联"]
        ThirdPay["第三方支付<br/>微信/支付宝"]
    end

    subgraph Platform["支付交易平台"]
        Gateway["API Gateway<br/>鉴权·限流·路由"]
        
        subgraph Core["payment-core"]
            OrderSvc["订单服务"]
            RouteSvc["渠道路由"]
            RefundSvc["退款服务"]
            ChannelAdapter["渠道适配器"]
        end
        
        subgraph Risk["risk-engine"]
            RiskEval["风控评估"]
            RuleEngine["规则引擎<br/>Drools"]
            FeatureStore["特征存储"]
            BlackWhiteList["黑白名单"]
        end
        
        subgraph Settle["settlement-service"]
            Reconcile["对账服务"]
            Clearing["清算服务"]
            Settlement["结算打款"]
            SplitAcct["分账服务"]
        end
        
        subgraph Portal["merchant-portal"]
            MerchantMgmt["商户管理"]
            KeyMgmt["密钥管理"]
            TxnQuery["交易查询"]
            Report["报表服务"]
        end
        
        subgraph Notify["notification-service"]
            NotifyDispatch["通知调度"]
            RetryEngine["重试引擎"]
        end
    end
    
    Consumer --> Gateway
    MerchantSys --> Gateway
    Gateway --> OrderSvc
    Gateway --> Portal
    OrderSvc --> RouteSvc
    OrderSvc --> RiskEval
    RouteSvc --> ChannelAdapter
    ChannelAdapter --> BankChannel
    ChannelAdapter --> ThirdPay
    OrderSvc --> NotifyDispatch
    RefundSvc --> ChannelAdapter
    Reconcile --> ChannelAdapter
    Settlement --> BankChannel
    TxnQuery --> OrderSvc
    Report --> Clearing
```

## 支付下单全流程时序图

```mermaid
sequenceDiagram
    autonumber
    participant Merchant as 商户系统
    participant GW as API Gateway
    participant PC as payment-core<br/>订单服务
    participant Route as payment-core<br/>渠道路由
    participant RE as risk-engine
    participant CH as 支付渠道<br/>微信/支付宝/银联
    participant NS as notification-service
    participant DB as 数据库

    Merchant->>GW: POST /api/v1/payments (订单信息+签名)
    GW->>GW: 验签 & 限流
    GW->>PC: CreatePayment(request)
    PC->>DB: 幂等校验(merchantOrderNo)
    
    alt 重复订单
        DB-->>PC: 已存在
        PC-->>GW: 返回已有订单
    else 新订单
        PC->>DB: 创建订单 (status=CREATED)
        PC->>RE: RiskEvaluate(orderId, amount, userInfo)
        
        alt 风控拒绝
            RE-->>PC: REJECT (riskLevel=HIGH, reason)
            PC->>DB: 更新订单 (status=RISK_REJECTED)
            PC-->>GW: 支付被拒(risk_rejected)
        else 风控通过
            RE-->>PC: PASS (riskLevel=LOW)
            PC->>Route: SelectChannel(amount, channelType, merchantConfig)
            Route-->>PC: 最优渠道(channelId, feeRate)
            PC->>DB: 更新订单 (status=PAYING, channelId)
            PC->>CH: 调用渠道支付API
            
            alt 渠道返回同步结果
                CH-->>PC: 支付成功
                PC->>DB: 更新订单 (status=SUCCESS)
                PC->>NS: SendNotification(orderId, SUCCESS)
                NS-->>Merchant: Webhook(支付成功)
            else 渠道异步回调
                CH-->>PC: 受理成功(pending)
                PC-->>GW: 支付受理中
                Note over CH,PC: 等待渠道异步回调...
                CH->>PC: 异步回调(支付结果)
                PC->>DB: 更新订单状态
                PC->>NS: SendNotification(orderId, result)
                NS-->>Merchant: Webhook(支付结果)
            end
        end
    end
    
    GW-->>Merchant: 统一响应(orderId, status, payUrl)
```

## 退款流程时序图

```mermaid
sequenceDiagram
    autonumber
    participant Merchant as 商户系统
    participant GW as API Gateway
    participant PC as payment-core<br/>退款服务
    participant CH as 支付渠道
    participant NS as notification-service
    participant DB as 数据库

    Merchant->>GW: POST /api/v1/refunds (原订单号+退款金额)
    GW->>PC: CreateRefund(request)
    PC->>DB: 查询原订单
    
    alt 原订单不存在或未成功
        PC-->>GW: 退款失败(原订单无效)
    else 退款金额超限
        PC-->>GW: 退款失败(金额超限)
    else 校验通过
        PC->>DB: 创建退款单(status=REFUNDING)
        PC->>CH: 调用渠道退款API
        CH-->>PC: 退款结果
        
        alt 退款成功
            PC->>DB: 更新退款单(status=SUCCESS)
            PC->>DB: 更新原订单退款金额
            PC->>NS: SendNotification(refundId, SUCCESS)
            NS-->>Merchant: Webhook(退款成功)
        else 退款失败
            PC->>DB: 更新退款单(status=FAILED)
            PC->>NS: SendNotification(refundId, FAILED)
            NS-->>Merchant: Webhook(退款失败)
        end
    end
    
    GW-->>Merchant: 退款响应(refundId, status)
```

## 支付订单状态机

```mermaid
stateDiagram-v2
    [*] --> CREATED: 商户下单
    CREATED --> RISK_REJECTED: 风控拒绝
    CREATED --> PAYING: 风控通过,发起支付
    PAYING --> SUCCESS: 支付成功
    PAYING --> FAILED: 支付失败
    PAYING --> TIMEOUT: 支付超时
    SUCCESS --> REFUNDING: 发起退款
    REFUNDING --> PARTIAL_REFUNDED: 部分退款成功
    REFUNDING --> FULL_REFUNDED: 全额退款成功
    PARTIAL_REFUNDED --> REFUNDING: 继续退款
    CREATED --> CLOSED: 商户关闭
    TIMEOUT --> CLOSED: 系统关闭
    RISK_REJECTED --> [*]
    FAILED --> [*]
    CLOSED --> [*]
    FULL_REFUNDED --> [*]

    note right of PAYING: 等待渠道返回结果<br/>超时默认30分钟
    note right of SUCCESS: 可退款有效期90天
```

## T+1 清结算流程图

```mermaid
flowchart TD
    A[T日交易截止 00:00] --> B[归集当日交易数据]
    B --> C[按渠道拉取渠道对账文件]
    C --> D{逐笔对账}
    
    D -->|一致| E[标记对账成功]
    D -->|不一致| F[标记差异]
    F --> G{差异类型}
    G -->|长款| H[平台多收, 挂账待处理]
    G -->|短款| I[平台少收, 发起调账]
    G -->|金额不符| J[人工审核]
    
    E --> K[按商户汇总]
    K --> L[计算手续费]
    L --> M{有分账配置?}
    
    M -->|是| N[计算分账金额]
    N --> O[生成清算明细]
    M -->|否| O
    
    O --> P[生成结算批次]
    P --> Q[T+1 发起银行打款]
    Q --> R{打款结果}
    
    R -->|成功| S[更新结算状态 SUCCESS]
    R -->|失败| T[重试/人工处理]
    
    S --> U[生成对账报表]
    U --> V[商户可下载对账文件]

    style A fill:#e1f5fe
    style S fill:#c8e6c9
    style T fill:#ffcdd2
```

## 领域实体关系图

```mermaid
erDiagram
    Merchant ||--o{ PaymentOrder : "发起支付"
    Merchant ||--o{ MerchantKey : "拥有密钥"
    Merchant ||--o{ SettlementBatch : "接收结算"
    
    PaymentOrder ||--o{ RefundOrder : "产生退款"
    PaymentOrder ||--|| Channel : "使用渠道"
    PaymentOrder ||--o| RiskEvent : "触发风控"
    PaymentOrder ||--o{ AccountFlow : "产生流水"
    PaymentOrder ||--o{ NotifyTask : "触发通知"
    
    Channel ||--o{ ChannelRoute : "配置路由"
    Channel ||--o{ ReconcileRecord : "对账记录"
    
    RiskRule ||--o{ RiskEvent : "命中规则"
    
    SettlementBatch ||--o{ AccountFlow : "包含流水"
    SettlementBatch ||--o{ ReconcileRecord : "关联对账"

    Merchant {
        string merchantId PK
        string name
        string status
        string settleAccount
        json feeConfig
    }
    
    PaymentOrder {
        string orderId PK
        string merchantId FK
        string merchantOrderNo UK
        decimal amount
        string currency
        string status
        string channelId FK
        string channelType
        timestamp createdAt
        timestamp paidAt
    }
    
    RefundOrder {
        string refundId PK
        string originalOrderId FK
        decimal refundAmount
        string status
        timestamp createdAt
    }
    
    RiskEvent {
        string eventId PK
        string orderId FK
        string riskLevel
        json ruleHits
        string action
        int score
    }
    
    SettlementBatch {
        string batchId PK
        string merchantId FK
        date settleDate
        decimal totalAmount
        decimal feeAmount
        string status
    }
    
    NotifyTask {
        string taskId PK
        string orderId FK
        string url
        string status
        int retryCount
        timestamp nextRetryAt
    }
```

## 事件目录

| 事件名称 | 发布者 | 消费者 | 触发时机 | 载体 |
|----------|--------|--------|----------|------|
| `PaymentCreated` | payment-core | risk-engine | 订单创建后 | Kafka |
| `PaymentSucceeded` | payment-core | settlement-service, notification-service | 支付成功 | Kafka |
| `PaymentFailed` | payment-core | notification-service | 支付失败 | Kafka |
| `RefundSucceeded` | payment-core | settlement-service, notification-service | 退款成功 | Kafka |
| `RiskEvaluated` | risk-engine | payment-core | 风控决策完成 | RPC Response |
| `SettlementCompleted` | settlement-service | notification-service, merchant-portal | 结算打款完成 | Kafka |
| `ReconcileDiffFound` | settlement-service | 运营告警 | 对账发现差异 | Kafka + 钉钉 |

## 集成契约概览

| 接口 | 协议 | 调用方 → 提供方 | 说明 |
|------|------|-----------------|------|
| `POST /api/v1/payments` | REST | merchant → payment-core | 统一下单 |
| `GET /api/v1/payments/{id}` | REST | merchant → payment-core | 订单查询 |
| `POST /api/v1/refunds` | REST | merchant → payment-core | 退款申请 |
| `RiskEvaluate(RiskRequest)` | gRPC | payment-core → risk-engine | 实时风控评估 |
| `SendNotification(NotifyRequest)` | Kafka | payment-core → notification-service | 异步通知 |
| `GetTransactions(QueryRequest)` | gRPC | settlement-service → payment-core | 拉取交易数据 |
| `GET /portal/api/v1/transactions` | REST | merchant-portal → payment-core | 交易查询 |
