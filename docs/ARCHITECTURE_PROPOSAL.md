# ACN 架构重组方案

## 🎯 目标

将 ACN 从"功能模块堆叠"重构为**清晰分层的企业级架构**。

---

## 📋 当前问题

### 1. **混乱的文件组织**
```
acn/
├── models.py          ⚠️ 单文件包含所有模型
├── config.py          ⚠️ 配置分散
├── registry.py        ⚠️ 单文件业务逻辑
├── a2a_integration.py ⚠️ 应该在 a2a/ 内
├── communication/     ✅ 目录结构
├── monitoring/        ✅ 目录结构
└── payments/          ✅ 目录结构
```

### 2. **缺少架构分层**
- ❌ 没有 Domain Layer (领域层)
- ❌ 没有 Service Layer (服务层)
- ❌ 没有 Schema Layer (数据验证层)
- ❌ 业务逻辑和数据访问混在一起

### 3. **依赖关系混乱**
- ❌ routes 直接调用 Registry/Router/Broadcast
- ❌ 缺少统一的服务抽象
- ❌ 难以测试和 mock

---

## 🏗️ 推荐架构：Clean Architecture + DDD Lite

### 参考框架

1. **FastAPI Best Practices** (推荐 ⭐⭐⭐⭐⭐)
   - GitHub: https://github.com/zhanymkanov/fastapi-best-practices
   - 轻量级，适合中型项目

2. **FastAPI + SQLAlchemy Template** (可选)
   - GitHub: https://github.com/tiangolo/full-stack-fastapi-template
   - FastAPI 官方推荐，但较重

3. **Clean Architecture Python** (理念参考)
   - 清晰的分层：Entities → Use Cases → Interface Adapters → Frameworks

---

## 🎨 新架构设计

### 目标结构

```
acn/
├── core/                      # 核心领域层 (Domain Layer)
│   ├── __init__.py
│   ├── entities/              # 领域实体
│   │   ├── __init__.py
│   │   ├── agent.py          # Agent 实体
│   │   ├── subnet.py         # Subnet 实体
│   │   └── message.py        # Message 实体
│   ├── exceptions.py          # 业务异常
│   └── interfaces/            # 接口定义 (抽象类)
│       ├── __init__.py
│       ├── registry.py       # IAgentRegistry 接口
│       ├── router.py         # IMessageRouter 接口
│       └── storage.py        # IStorage 接口
│
├── schemas/                   # API 数据模型 (Pydantic)
│   ├── __init__.py
│   ├── agent.py              # Agent 相关 schema
│   ├── message.py            # Message schema
│   ├── subnet.py             # Subnet schema
│   └── common.py             # 通用 schema
│
├── services/                  # 业务逻辑层 (Use Cases)
│   ├── __init__.py
│   ├── agent_service.py      # Agent 业务逻辑
│   ├── message_service.py    # 消息路由业务逻辑
│   ├── broadcast_service.py  # 广播业务逻辑
│   └── subnet_service.py     # 子网业务逻辑
│
├── infrastructure/            # 基础设施层 (实现)
│   ├── __init__.py
│   ├── persistence/          # 数据持久化
│   │   ├── __init__.py
│   │   ├── redis/
│   │   │   ├── __init__.py
│   │   │   ├── registry.py   # Redis Registry 实现
│   │   │   └── cache.py      # Redis Cache
│   │   └── postgres/         # (Future)
│   │       └── __init__.py
│   ├── messaging/            # 消息队列
│   │   ├── __init__.py
│   │   ├── router.py         # 消息路由实现
│   │   └── queue.py          # 队列管理
│   └── external/             # 外部服务
│       ├── __init__.py
│       ├── auth0.py          # Auth0 集成
│       └── webhooks.py       # Webhook 客户端
│
├── protocols/                 # 协议适配层
│   ├── __init__.py
│   ├── a2a/                  # A2A 协议
│   │   ├── __init__.py
│   │   ├── server.py         # A2A Server
│   │   ├── executor.py       # ACN Executor
│   │   ├── handlers.py       # Action handlers
│   │   └── task_store.py     # Redis Task Store
│   └── ap2/                  # AP2 协议 (Future)
│       └── __init__.py
│
├── api/                       # API 层 (Interface Adapters)
│   ├── __init__.py
│   ├── app.py                # FastAPI app factory
│   ├── dependencies.py       # 依赖注入
│   ├── middleware/           # 中间件
│   │   ├── __init__.py
│   │   ├── auth.py
│   │   ├── cors.py
│   │   └── logging.py
│   └── routes/               # 路由
│       ├── __init__.py
│       ├── v1/               # API v1
│       │   ├── __init__.py
│       │   ├── agents.py
│       │   ├── messages.py
│       │   ├── subnets.py
│       │   └── health.py
│       └── websocket.py      # WebSocket 端点
│
├── monitoring/                # 监控 (保持现有)
│   ├── __init__.py
│   ├── metrics.py
│   ├── analytics.py
│   └── audit.py
│
└── config/                    # 配置管理
    ├── __init__.py
    ├── settings.py           # 主配置
    ├── environments/         # 环境配置
    │   ├── __init__.py
    │   ├── development.py
    │   ├── production.py
    │   └── testing.py
    └── logging.py            # 日志配置
```

---

## 🔄 迁移步骤

### Phase 1: 创建新结构 (2-3 小时)

```bash
# 1. 创建核心目录
mkdir -p acn/core/{entities,interfaces}
mkdir -p acn/schemas
mkdir -p acn/services
mkdir -p acn/infrastructure/{persistence/redis,messaging,external}
mkdir -p acn/protocols/a2a
mkdir -p acn/api/{middleware,routes/v1}
mkdir -p acn/config/environments

# 2. 创建 __init__.py
find acn/core acn/schemas acn/services acn/infrastructure acn/protocols -type d -exec touch {}/__init__.py \;
```

### Phase 2: 迁移数据模型 (1-2 小时)

**从 `models.py` 拆分到 `schemas/` 和 `core/entities/`**

```python
# 旧: acn/models.py (250 行)
class AgentInfo(BaseModel):
    agent_id: str
    name: str
    ...

# 新: acn/core/entities/agent.py (领域实体)
@dataclass
class Agent:
    """Agent Domain Entity"""
    id: str
    name: str
    owner: str
    ...
    
    def is_online(self) -> bool:
        """Business logic here"""
        ...

# 新: acn/schemas/agent.py (API Schema)
class AgentResponse(BaseModel):
    """Agent API Response"""
    agent_id: str
    name: str
    status: str
    ...
```

### Phase 3: 提取服务层 (2-3 小时)

**将业务逻辑从 routes 移到 services**

```python
# 旧: acn/routes/registry.py (直接调用 Registry)
@router.post("/register")
async def register_agent(
    request: AgentRegisterRequest,
    registry: RegistryDep,
):
    agent_id = await registry.register_agent(...)  # ❌ 直接调用基础设施
    return AgentRegisterResponse(agent_id=agent_id)

# 新: acn/services/agent_service.py (业务逻辑层)
class AgentService:
    def __init__(self, registry: IAgentRegistry):
        self.registry = registry
    
    async def register_agent(
        self, 
        request: RegisterAgentCommand
    ) -> Agent:
        """Register agent with validation and business rules"""
        # ✅ 业务逻辑在这里
        # 1. 验证
        # 2. 重复检查
        # 3. 调用 registry
        # 4. 发送事件
        ...
        return agent

# 新: acn/api/routes/v1/agents.py (只负责 HTTP)
@router.post("/register")
async def register_agent(
    request: AgentRegisterRequest,
    service: AgentServiceDep,  # ✅ 依赖服务层
):
    agent = await service.register_agent(
        RegisterAgentCommand.from_request(request)
    )
    return AgentResponse.from_entity(agent)
```

### Phase 4: 重构基础设施 (2-3 小时)

**将实现移到 infrastructure/**

```python
# 新: acn/core/interfaces/registry.py (接口定义)
class IAgentRegistry(ABC):
    @abstractmethod
    async def save(self, agent: Agent) -> None:
        ...
    
    @abstractmethod
    async def find_by_id(self, agent_id: str) -> Agent | None:
        ...

# 新: acn/infrastructure/persistence/redis/registry.py (Redis 实现)
class RedisAgentRegistry(IAgentRegistry):
    def __init__(self, redis: Redis):
        self.redis = redis
    
    async def save(self, agent: Agent) -> None:
        """Redis-specific implementation"""
        ...
```

### Phase 5: 整合协议层 (1-2 小时)

**移动 A2A 相关代码**

```bash
# 移动文件
mv acn/a2a_integration.py acn/protocols/a2a/server.py
mv acn/a2a/redis_task_store.py acn/protocols/a2a/task_store.py

# 拆分 server.py
# → executor.py (ACNAgentExecutor)
# → handlers.py (_handle_* methods)
```

---

## 📊 架构对比

### Before (当前)
```
Request → Route → Repository → Redis
         (All in one)
```

### After (推荐)
```
Request → Route (API Layer)
         ↓
       Service (Business Logic)
         ↓
       Repository Interface (Core)
         ↓
       Redis Implementation (Infrastructure)
```

---

## ✅ 优势

### 1. **清晰的职责分离**
- **Core**: 不依赖任何框架，纯业务逻辑
- **Services**: 编排业务流程
- **Infrastructure**: 具体实现可替换
- **API**: 只负责 HTTP 层

### 2. **易于测试**
```python
# 测试业务逻辑（不需要 Redis）
def test_register_agent():
    mock_registry = Mock(spec=IAgentRegistry)
    service = AgentService(mock_registry)
    
    result = await service.register_agent(...)
    
    assert result.name == "test"
    mock_registry.save.assert_called_once()
```

### 3. **易于扩展**
```python
# 添加新的存储实现
class PostgresAgentRegistry(IAgentRegistry):
    """切换到 Postgres，业务逻辑不变"""
    ...

# 依赖注入时替换
app.dependency_overrides[IAgentRegistry] = PostgresAgentRegistry
```

### 4. **更好的可读性**
```
acn/services/agent_service.py        ← 看这里了解业务逻辑
acn/infrastructure/redis/registry.py  ← 看这里了解存储细节
acn/api/routes/v1/agents.py          ← 看这里了解 API 定义
```

---

## 🎯 快速启动：最小可行方案 (MVP)

如果完整重构工作量太大，可以先做**渐进式改进**：

### Step 1: 拆分 models.py (1 小时)
```bash
mkdir acn/schemas
# 移动 Pydantic models
mv acn/models.py acn/schemas/models.py
```

### Step 2: 添加服务层 (2 小时)
```bash
mkdir acn/services
# 创建 agent_service.py, message_service.py
# 将核心逻辑从 routes 迁移过来
```

### Step 3: 移动 a2a_integration.py (30 分钟)
```bash
mv acn/a2a_integration.py acn/a2a/server.py
```

### Step 4: 统一配置 (30 分钟)
```bash
mkdir acn/config
mv acn/config.py acn/config/settings.py
```

---

## 🛠️ 推荐工具

### 1. **Cookiecutter 模板**
```bash
# 使用现成的 FastAPI 项目模板
pip install cookiecutter
cookiecutter gh:tiangolo/full-stack-fastapi-template
```

### 2. **依赖注入框架**
```bash
# 使用 dependency-injector 管理依赖
pip install dependency-injector
```

### 3. **架构验证工具**
```bash
# 使用 import-linter 强制架构边界
pip install import-linter
```

---

## 📚 参考资源

1. **FastAPI Best Practices**
   - https://github.com/zhanymkanov/fastapi-best-practices

2. **Clean Architecture in Python**
   - https://github.com/cosmic-python/book

3. **Python Microservices Development**
   - https://github.com/PacktPublishing/Python-Microservices-Development

4. **Domain-Driven Design**
   - https://github.com/ddd-crew/ddd-starter-modelling-process

---

## 🤔 我的建议

基于 ACN 的现状（8500+ 行代码，已有功能），我建议：

### 方案 A: **渐进式重构** (推荐 ⭐⭐⭐⭐⭐)
- 时间：1-2 周
- 风险：低
- 步骤：
  1. 先拆分 models.py → schemas/
  2. 添加 services/ 层
  3. 整理 a2a/ 目录
  4. 逐步迁移其他模块

### 方案 B: **全面重构**
- 时间：3-4 周
- 风险：中
- 步骤：按照上面的完整架构重构

### 方案 C: **保持现状 + 小优化**
- 时间：2-3 天
- 风险：极低
- 步骤：只做最小调整（移动文档、修复嵌套）

---

**你倾向于哪个方案？** 我可以帮你立即开始实施。

