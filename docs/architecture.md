# ACN 架构文档

ACN (Agent Collaboration Network) 的系统架构设计。

---

## 系统概览

```
                            ┌──────────────────────────────────────┐
                            │           External Clients            │
                            │    (Agents, Applications, Admin)      │
                            └──────────────────┬───────────────────┘
                                               │
                                               ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                              ACN Server                                       │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │                          FastAPI Application                            │  │
│  │                                                                         │  │
│  │   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐   │  │
│  │   │  Registry   │  │Communication│  │  Payments   │  │ Monitoring  │   │  │
│  │   │   Module    │  │   Module    │  │   Module    │  │   Module    │   │  │
│  │   └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘   │  │
│  │          │                │                │                │          │  │
│  │   ┌──────┴────────────────┴────────────────┴────────────────┴──────┐   │  │
│  │   │                     Subnet Manager                              │   │  │
│  │   │         (Multi-Subnet Support, Gateway, Auth)                   │   │  │
│  │   └─────────────────────────────┬───────────────────────────────────┘   │  │
│  │                                 │                                       │  │
│  └─────────────────────────────────┼───────────────────────────────────────┘  │
│                                    │                                          │
│  ┌─────────────────────────────────┴───────────────────────────────────────┐  │
│  │                            Redis Storage                                 │  │
│  │   • Agent Registry    • Message Queues    • Payment Tasks    • Metrics  │  │
│  └──────────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────┘
                                               │
                                               ▼
                            ┌──────────────────────────────────────┐
                            │        External Services              │
                            │  • A2A Agents  • Payment Processors   │
                            │  • Webhook Endpoints  • Prometheus    │
                            └──────────────────────────────────────┘
```

---

## 模块架构

### 1. Registry Module (`acn/registry.py`)

Agent 注册与发现核心模块。

```
┌─────────────────────────────────────────┐
│            AgentRegistry                 │
├─────────────────────────────────────────┤
│ + register_agent()                       │
│ + unregister_agent()                     │
│ + get_agent()                            │
│ + search_agents()                        │
│ + heartbeat()                            │
│ + add_agent_to_subnet()                  │
│ + remove_agent_from_subnet()             │
├─────────────────────────────────────────┤
│ Redis Keys:                              │
│  • acn:agents:{agent_id}     (Hash)     │
│  • acn:agents:all            (Set)      │
│  • acn:skills:{skill}        (Set)      │
│  • acn:subnet:{subnet_id}    (Set)      │
└─────────────────────────────────────────┘
```

**职责**:
- Agent CRUD 操作
- Agent Card 生成与托管
- 技能索引与搜索
- 多子网成员管理

### 2. Communication Module (`acn/communication/`)

A2A 协议通信层。

```
┌─────────────────────────────────────────────────────────────────┐
│                    Communication Module                          │
├─────────────────┬─────────────────┬─────────────────────────────┤
│  MessageRouter  │ BroadcastService│    WebSocketManager          │
├─────────────────┼─────────────────┼─────────────────────────────┤
│ • route()       │ • broadcast()   │ • connect()                  │
│ • route_by_skill│ • parallel      │ • disconnect()               │
│ • send_message  │ • sequential    │ • subscribe()                │
│                 │ • first_response│ • broadcast()                │
└─────────────────┴─────────────────┴─────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    SubnetManager (Gateway)                       │
├─────────────────────────────────────────────────────────────────┤
│ • create_subnet()      • handle_gateway_connection()             │
│ • delete_subnet()      • forward_request()                       │
│ • validate_token()     • cross_subnet_routing()                  │
└─────────────────────────────────────────────────────────────────┘
```

**组件说明**:

| 组件 | 职责 |
|-----|------|
| `MessageRouter` | A2A 消息路由，按 Agent ID 或技能路由 |
| `BroadcastService` | 多目标消息广播，支持多种策略 |
| `WebSocketManager` | WebSocket 连接管理，实时消息推送 |
| `SubnetManager` | 子网生命周期，Gateway 跨网通信 |

### 3. Payments Module (`acn/payments/`)

AP2 协议支付集成。

```
┌─────────────────────────────────────────────────────────────────┐
│                      Payments Module                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌───────────────────┐    ┌───────────────────┐                 │
│  │ PaymentDiscovery  │    │ PaymentTaskManager│                 │
│  │    Service        │    │                   │                 │
│  ├───────────────────┤    ├───────────────────┤                 │
│  │ • index_capability│    │ • create_task()   │                 │
│  │ • find_by_method  │    │ • update_status() │                 │
│  │ • find_by_network │    │ • get_task()      │                 │
│  │ • get_capability  │    │ • get_stats()     │                 │
│  └───────────────────┘    └─────────┬─────────┘                 │
│                                     │                            │
│                                     ▼                            │
│                          ┌───────────────────┐                  │
│                          │  WebhookService   │                  │
│                          ├───────────────────┤                  │
│                          │ • send_event()    │                  │
│                          │ • retry_delivery()│                  │
│                          │ • get_history()   │                  │
│                          └───────────────────┘                  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

**支付任务状态机**:

```
            ┌─────────────────────────────────────────────────────┐
            │                                                     │
            ▼                                                     │
        ┌───────┐    ┌─────────────┐    ┌─────────────┐    ┌─────┴─────┐
        │CREATED│───▶│PAY_REQUESTED│───▶│PAY_PENDING  │───▶│PAY_CONFIRMED│
        └───────┘    └─────────────┘    └─────────────┘    └─────┬─────┘
                                                                 │
                                                                 ▼
                     ┌─────────────┐    ┌─────────────┐    ┌───────────┐
                     │PAY_RELEASED │◀───│TASK_COMPLETE│◀───│IN_PROGRESS│
                     └─────────────┘    └─────────────┘    └───────────┘
                           │
            ┌──────────────┼──────────────┐
            ▼              ▼              ▼
        ┌───────┐    ┌─────────┐    ┌──────────┐
        │DISPUTED│    │CANCELLED│    │  FAILED  │
        └───────┘    └─────────┘    └──────────┘
```

### 4. Monitoring Module (`acn/monitoring/`)

可观测性与审计。

```
┌─────────────────────────────────────────────────────────────────┐
│                     Monitoring Module                            │
├─────────────────┬─────────────────┬─────────────────────────────┤
│ MetricsCollector│   AuditLogger   │      Analytics              │
├─────────────────┼─────────────────┼─────────────────────────────┤
│ • inc_counter() │ • log_event()   │ • get_agent_stats()         │
│ • set_gauge()   │ • query_events()│ • get_message_stats()       │
│ • observe()     │ • export()      │ • get_dashboard()           │
│ • prometheus()  │ • count_events()│ • generate_report()         │
└─────────────────┴─────────────────┴─────────────────────────────┘
```

**Prometheus 指标**:

| 指标 | 类型 | 说明 |
|-----|------|------|
| `acn_agents_total` | Gauge | 注册 Agent 总数 |
| `acn_agents_online` | Gauge | 在线 Agent 数 |
| `acn_messages_total` | Counter | 消息总数 |
| `acn_message_latency_seconds` | Histogram | 消息延迟 |
| `acn_subnets_total` | Gauge | 子网数量 |
| `acn_payment_tasks_total` | Counter | 支付任务数 |

---

## 数据模型

### AgentInfo

```python
class AgentInfo(BaseModel):
    agent_id: str
    name: str
    description: str = ""
    endpoint: str
    skills: list[str] = []
    status: str = "online"
    subnet_ids: list[str] = ["public"]
    agent_card: AgentCard | None = None
    metadata: dict = {}
    registered_at: datetime
    last_heartbeat: datetime | None = None
    wallet_address: str | None = None
    payment_capability: PaymentCapability | None = None
```

### SubnetInfo

```python
class SubnetInfo(BaseModel):
    subnet_id: str
    name: str
    description: str = ""
    metadata: dict = {}
    security_schemes: dict | None = None
    default_security: list[dict] | None = None
    created_at: datetime
```

### PaymentTask

```python
class PaymentTask(BaseModel):
    task_id: str
    payment_id: str | None = None
    buyer_agent: str
    seller_agent: str
    task_description: str
    task_type: str | None = None
    amount: str
    currency: str = "USD"
    payment_method: SupportedPaymentMethod | None = None
    network: SupportedNetwork | None = None
    recipient_wallet: str | None = None
    status: PaymentTaskStatus
    created_at: datetime
    tx_hash: str | None = None
```

---

## Redis 数据结构

### Agent 数据

```
# Agent 详情 (Hash)
acn:agents:{agent_id}
├── agent_id: "my-agent"
├── name: "My Agent"
├── endpoint: "http://localhost:8001"
├── skills: '["coding", "analysis"]'  # JSON
├── subnet_ids: '["public", "team-a"]'  # JSON
├── status: "online"
├── agent_card: '{...}'  # JSON
├── payment_capability: '{...}'  # JSON
└── registered_at: "2024-01-15T10:30:00Z"

# 全部 Agent (Set)
acn:agents:all -> {"agent-1", "agent-2", ...}

# 技能索引 (Set)
acn:skills:{skill} -> {"agent-1", "agent-3", ...}

# 子网成员 (Set)
acn:subnet:{subnet_id}:agents -> {"agent-1", "agent-2", ...}
```

### 支付数据

```
# 支付任务 (String - JSON)
acn:payment_tasks:{task_id} -> '{...}'

# Agent 支付能力索引 (Set)
acn:payments:by_method:{method} -> {"agent-1", "agent-2"}
acn:payments:by_network:{network} -> {"agent-1", "agent-3"}

# 支付统计 (Hash)
acn:payments:stats:{agent_id}
├── total_as_buyer: "5"
├── total_as_seller: "10"
└── amount_usd: "1500.00"
```

### WebSocket 数据

```
# 连接信息 (Hash)
acn:ws:connections:{connection_id}
├── agent_id: "my-agent"
├── subnet_id: "public"
└── connected_at: "2024-01-15T10:30:00Z"

# Agent 订阅 (Set)
acn:ws:subscriptions:{agent_id} -> {"topic-1", "topic-2"}
```

---

## 部署架构

### 单节点部署

```
┌─────────────────────────────────────────┐
│              Docker Host                 │
│                                          │
│  ┌────────────┐    ┌────────────────┐   │
│  │   Redis    │◀───│   ACN Server   │   │
│  │  :6379     │    │    :8000       │   │
│  └────────────┘    └────────────────┘   │
│                           │              │
└───────────────────────────┼──────────────┘
                            │
                            ▼
                    External Clients
```

```bash
docker-compose up -d
```

### 高可用部署

```
                    ┌──────────────┐
                    │ Load Balancer│
                    └──────┬───────┘
                           │
          ┌────────────────┼────────────────┐
          │                │                │
          ▼                ▼                ▼
    ┌──────────┐    ┌──────────┐    ┌──────────┐
    │ACN Node 1│    │ACN Node 2│    │ACN Node 3│
    └────┬─────┘    └────┬─────┘    └────┬─────┘
         │               │               │
         └───────────────┼───────────────┘
                         │
                         ▼
              ┌──────────────────┐
              │  Redis Cluster   │
              │   (3 masters)    │
              └──────────────────┘
```

### Kubernetes 部署

```yaml
# deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: acn-server
spec:
  replicas: 3
  template:
    spec:
      containers:
      - name: acn
        image: acn:latest
        ports:
        - containerPort: 8000
        env:
        - name: REDIS_URL
          valueFrom:
            secretKeyRef:
              name: acn-secrets
              key: redis-url
```

---

## 安全设计

### 子网认证

```
┌─────────────────────────────────────────────────────────────┐
│                    Authentication Flow                       │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  1. 创建子网时生成 Token                                     │
│     POST /subnets → {token: "sk_subnet_xxx"}                │
│                                                              │
│  2. 访问私有子网 API 需携带 Token                            │
│     Authorization: Bearer sk_subnet_xxx                      │
│                                                              │
│  3. ACN 验证 Token 并授权访问                                │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Webhook 签名

```python
# Webhook 请求签名
signature = hmac.new(
    secret.encode(),
    payload_json.encode(),
    hashlib.sha256
).hexdigest()

# 请求头
X-ACN-Signature: sha256={signature}
X-ACN-Timestamp: 2024-01-15T10:30:00Z
```

---

## 扩展点

### 自定义存储后端

```python
class StorageBackend(Protocol):
    async def get(self, key: str) -> Any: ...
    async def set(self, key: str, value: Any) -> None: ...
    async def delete(self, key: str) -> None: ...

# 可实现 PostgreSQL、MongoDB 等后端
```

### 自定义认证

```python
class AuthProvider(Protocol):
    async def validate_token(self, token: str) -> bool: ...
    async def get_permissions(self, token: str) -> list[str]: ...

# 可集成 OAuth2、JWT、LDAP 等
```

### 自定义支付处理器

```python
class PaymentProcessor(Protocol):
    async def create_payment(self, task: PaymentTask) -> str: ...
    async def confirm_payment(self, payment_id: str) -> bool: ...
    async def refund_payment(self, payment_id: str) -> bool: ...

# 可集成 Stripe、PayPal、链上支付等
```

---

## 更多资源

- [API 文档](./api.md)
- [README](../README.md)
- [A2A Protocol](https://github.com/google/A2A)
- [AP2 Protocol](https://github.com/google-agentic-commerce/AP2)

