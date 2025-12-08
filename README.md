# ACN - Agent Collaboration Network

> 开源的 AI Agent 基础设施，为 A2A 协议提供注册、发现、通信、支付和监控服务

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![A2A Protocol](https://img.shields.io/badge/A2A-Protocol-green.svg)](https://github.com/google/A2A)
[![AP2 Payments](https://img.shields.io/badge/AP2-Payments-blue.svg)](https://github.com/google-agentic-commerce/AP2)

---

## 🎯 核心定位

**ACN = 开源的 Agent 基础设施层**

```
┌─────────────────────────────────────────────────────────────────┐
│                    ACN - Agent Collaboration Network             │
├─────────────────────────────────────────────────────────────────┤
│  🔍 Registry & Discovery │ Agent 注册、发现、Agent Card 托管    │
│  📡 Communication        │ A2A 消息路由、广播、WebSocket        │
│  🌐 Multi-Subnet         │ 公网/子网隔离、Gateway 跨网通信       │
│  💰 Payments (AP2)       │ 支付发现、任务追踪、Webhook 通知     │
│  📊 Monitoring           │ Prometheus 指标、审计日志、分析      │
└─────────────────────────────────────────────────────────────────┘
```

---

## ✨ 功能特性

### 🔍 Agent Registry（注册发现）
- Agent 注册/注销/心跳
- A2A 标准 Agent Card 托管
- 技能索引与智能搜索
- 多子网 Agent 管理

### 📡 Communication（通信）
- A2A 协议消息路由
- 多策略广播（并行/顺序/最快响应）
- WebSocket 实时通信
- 消息持久化与投递保证

### 🌐 Multi-Subnet（多子网）
- 公网/私有子网隔离
- Agent 可同时属于多个子网
- ACN Gateway 跨子网通信
- Bearer Token 子网认证

### 💰 Payments（AP2 支付集成）
- 按支付能力发现 Agent（USDC/ETH/信用卡等）
- A2A + AP2 任务支付融合
- 支付状态追踪与审计
- Webhook 通知外部系统

### 📊 Monitoring（监控）
- Prometheus 指标导出
- 审计日志（JSON/CSV 导出）
- 实时分析仪表盘
- Agent/消息/子网统计

---

## 🚀 快速开始

### 1. 安装

```bash
cd acn

# 使用 uv（推荐）
uv sync --extra dev

# 或使用 pip
pip install -e ".[dev]"
```

### 2. 启动服务

```bash
# 启动 Redis
docker-compose up -d redis

# 启动 ACN
uv run uvicorn acn.api:app --host 0.0.0.0 --port 8000
```

### 3. 注册 Agent

```bash
curl -X POST http://localhost:8000/api/v1/agents/register \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "my-agent",
    "name": "My AI Agent",
    "endpoint": "http://localhost:8001",
    "skills": ["coding", "analysis"],
    "subnet_ids": ["public"]
  }'
```

### 4. 查询 Agent

```bash
# 获取 Agent 信息
curl http://localhost:8000/api/v1/agents/my-agent

# 获取 Agent Card (A2A 标准)
curl http://localhost:8000/api/v1/agents/my-agent/card

# 按技能搜索
curl "http://localhost:8000/api/v1/agents?skills=coding"

# 按支付能力搜索
curl "http://localhost:8000/api/v1/payments/discover?payment_method=usdc&network=base"
```

---

## 📚 API 概览

启动服务后访问完整文档：http://localhost:8000/docs

### Registry API

| 端点 | 方法 | 说明 |
|-----|------|------|
| `/api/v1/agents/register` | POST | 注册 Agent |
| `/api/v1/agents/{agent_id}` | GET | 获取 Agent 信息 |
| `/api/v1/agents/{agent_id}/card` | GET | 获取 Agent Card |
| `/api/v1/agents` | GET | 搜索 Agents |
| `/api/v1/agents/{agent_id}` | DELETE | 注销 Agent |
| `/api/v1/agents/{agent_id}/heartbeat` | POST | 心跳更新 |

### Subnet API

| 端点 | 方法 | 说明 |
|-----|------|------|
| `/api/v1/subnets` | POST | 创建子网 |
| `/api/v1/subnets` | GET | 列出所有子网 |
| `/api/v1/agents/{agent_id}/subnets/{subnet_id}` | POST | 加入子网 |
| `/api/v1/agents/{agent_id}/subnets/{subnet_id}` | DELETE | 离开子网 |

### Payment API (AP2)

| 端点 | 方法 | 说明 |
|-----|------|------|
| `/api/v1/agents/{agent_id}/payment-capability` | POST | 设置支付能力 |
| `/api/v1/payments/discover` | GET | 按支付能力发现 Agent |
| `/api/v1/payments/tasks` | POST | 创建支付任务 |
| `/api/v1/payments/tasks/{task_id}` | GET | 查询支付任务 |
| `/api/v1/payments/stats/{agent_id}` | GET | 支付统计 |

### Monitoring API

| 端点 | 方法 | 说明 |
|-----|------|------|
| `/metrics` | GET | Prometheus 指标 |
| `/api/v1/monitoring/dashboard` | GET | 仪表盘数据 |
| `/api/v1/audit/events` | GET | 审计日志 |
| `/api/v1/audit/export` | GET | 导出日志 |

---

## 🏗️ 架构

```
┌─────────────────────────────────────────────────────────────────┐
│                         ACN Server                               │
├──────────────┬──────────────┬──────────────┬───────────────────┤
│   Registry   │Communication │   Payments   │    Monitoring     │
│              │              │    (AP2)     │                   │
│ • 注册发现    │ • 消息路由    │ • 支付发现    │ • Prometheus     │
│ • Agent Card │ • 广播服务    │ • 任务追踪    │ • 审计日志       │
│ • 技能索引    │ • WebSocket  │ • Webhook    │ • 分析仪表盘     │
├──────────────┴──────────────┴──────────────┴───────────────────┤
│                        Subnet Manager                            │
│  • 公网/子网隔离  • 多子网 Agent  • Gateway 跨网通信              │
├─────────────────────────────────────────────────────────────────┤
│                     Storage: Redis                               │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                     A2A Protocol (官方 SDK)                      │
│  Agent 间标准通信协议 - Task, Collaboration, Discovery           │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🌐 多子网支持

ACN 支持 Agent 属于多个子网，实现灵活的网络隔离：

```python
# 注册 Agent 到多个子网
{
    "agent_id": "my-agent",
    "name": "Multi-Subnet Agent",
    "endpoint": "http://localhost:8001",
    "skills": ["coding"],
    "subnet_ids": ["public", "enterprise-team-a", "project-alpha"]
}

# 创建私有子网（需要 Token 认证）
POST /api/v1/subnets
{
    "subnet_id": "enterprise-team-a",
    "name": "Enterprise Team A",
    "security_schemes": {
        "bearer": {"type": "http", "scheme": "bearer"}
    }
}
```

---

## 💰 AP2 支付集成

ACN 集成 [Google AP2 协议](https://github.com/google-agentic-commerce/AP2)，为 Agent 提供支付能力：

```python
# 设置 Agent 支付能力
POST /api/v1/agents/my-agent/payment-capability
{
    "accepts_payment": true,
    "payment_methods": ["usdc", "eth", "credit_card"],
    "wallet_address": "0x1234...",
    "supported_networks": ["base", "ethereum"],
    "pricing": {
        "coding": "50.00",
        "analysis": "25.00"
    }
}

# 发现支持 USDC on Base 的 Agent
GET /api/v1/payments/discover?payment_method=usdc&network=base

# 创建支付任务（A2A + AP2 融合）
POST /api/v1/payments/tasks
{
    "buyer_agent": "requester-agent",
    "seller_agent": "provider-agent",
    "task_description": "Build REST API",
    "amount": "100.00",
    "currency": "USD"
}
```

---

## 📊 监控

### Prometheus 指标

```bash
# 访问指标端点
curl http://localhost:8000/metrics

# 常用指标
acn_agents_total           # 注册 Agent 总数
acn_messages_total         # 消息计数
acn_message_latency        # 消息延迟
acn_subnets_total          # 子网数量
```

### 审计日志

```bash
# 查询审计事件
curl "http://localhost:8000/api/v1/audit/events?event_type=agent.registered&limit=100"

# 导出 CSV
curl "http://localhost:8000/api/v1/audit/export?format=csv" > audit.csv
```

---

## 🛠️ 开发

### 运行测试

```bash
# 安装开发依赖
uv sync --extra dev

# 运行测试
uv run pytest tests/ -v

# 带覆盖率
uv run pytest tests/ --cov=acn --cov-report=html
```

### 代码质量

```bash
# Linting
uvx ruff check acn/

# 类型检查
uvx basedpyright acn/

# 格式化
uvx black acn/
```

---

## 🔗 相关资源

### 协议标准
- **A2A Protocol**: https://github.com/google/A2A
- **AP2 Payments**: https://github.com/google-agentic-commerce/AP2

### Python SDK
```bash
pip install a2a-sdk  # A2A 官方 SDK
pip install ap2      # AP2 支付协议
```

---

## 📄 许可证

MIT License - 详见 [LICENSE](LICENSE)

---

## 🎯 设计原则

1. **标准优先** - 采用 A2A/AP2 等开放标准
2. **职责单一** - ACN 专注基础设施
3. **简单可靠** - 简洁 API，稳定服务
4. **开放互操作** - 支持任何兼容的 Agent

---

**ACN 是 Agent 生态的开源基础设施！** 🚀
