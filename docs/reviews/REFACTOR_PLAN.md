# ACN API 重构计划

> ⚠️ **历史快照**：本文档是 2024-12 的 `api.py` 模块化拆分计划。文中所有 `from ..registry import AgentRegistry` / `Depends(get_registry)` 的代码示例都已失效——`AgentRegistry` 类已于 2026-05（commit `4771a1b`）删除，依赖注入改为 `AgentServiceDep`。最新调用约定见 `docs/agent-registry-removal.md` 与 `.github/CONTRIBUTING.md` 的"Writing Tests"章节。本文保留作为历史档案。

## 🎯 目标

将 `api.py`（1794行）拆分为模块化的路由结构，提升可维护性。

## 📋 执行步骤

### Step 1: 创建新目录结构（5分钟）

```bash
mkdir -p acn/api/routes
touch acn/api/__init__.py
touch acn/api/routes/__init__.py
touch acn/api/routes/registry.py
touch acn/api/routes/communication.py
touch acn/api/routes/subnets.py
touch acn/api/routes/monitoring.py
touch acn/api/routes/analytics.py
touch acn/api/routes/payments.py
touch acn/api/routes/websocket.py
touch acn/api/dependencies.py
```

### Step 2: 提取共享依赖（30分钟）

**创建 `acn/api/dependencies.py`**：

```python
"""FastAPI 依赖注入"""
from typing import AsyncGenerator

from fastapi import Depends, Header, HTTPException
from redis.asyncio import Redis

from ..auth.middleware import verify_token
from ..config import get_settings
from ..registry import AgentRegistry
from ..communication import BroadcastService, MessageRouter, SubnetManager
from ..monitoring import AnalyticsService, AuditService, MetricsCollector

settings = get_settings()

# Redis 连接池
async def get_redis() -> AsyncGenerator[Redis, None]:
    redis = Redis.from_url(settings.redis_url)
    try:
        yield redis
    finally:
        await redis.close()

# 核心服务
async def get_registry(redis: Redis = Depends(get_redis)) -> AgentRegistry:
    return AgentRegistry(redis)

async def get_broadcast_service(
    redis: Redis = Depends(get_redis),
    registry: AgentRegistry = Depends(get_registry)
) -> BroadcastService:
    return BroadcastService(redis, registry)

# ... 其他依赖
```

### Step 3: 拆分路由模块（2小时）

#### **acn/api/routes/registry.py** - Agent 注册相关

```python
"""Agent Registry API Routes"""
from fastapi import APIRouter, Depends, HTTPException

from ...models import AgentRegisterRequest, AgentRegisterResponse
from ..dependencies import get_registry, verify_token

router = APIRouter(prefix="/api/v1/agents", tags=["registry"])

@router.post("/register", response_model=AgentRegisterResponse)
async def register_agent(
    request: AgentRegisterRequest,
    token: dict = Depends(verify_token),
    registry: AgentRegistry = Depends(get_registry),
):
    """Register a new agent"""
    # 移动自 api.py 第 264-339 行
    ...

@router.get("/{agent_id}", response_model=AgentInfo)
async def get_agent(
    agent_id: str,
    registry: AgentRegistry = Depends(get_registry),
):
    """Get agent info"""
    # 移动自 api.py 第 340-354 行
    ...

# ... 其他 registry 路由
```

#### **acn/api/routes/communication.py** - 通信相关

```python
"""Communication API Routes"""
from fastapi import APIRouter, Depends

from ...models import SendMessageRequest, BroadcastRequest
from ..dependencies import get_message_router, get_broadcast_service

router = APIRouter(prefix="/api/v1/communication", tags=["communication"])

@router.post("/send")
async def send_message(
    request: SendMessageRequest,
    router: MessageRouter = Depends(get_message_router),
):
    """Send message to agent"""
    # 移动自 api.py 第 654-703 行
    ...

@router.post("/broadcast")
async def broadcast_message(
    request: BroadcastRequest,
    broadcast: BroadcastService = Depends(get_broadcast_service),
):
    """Broadcast message to multiple agents"""
    # 移动自 api.py 第 704-743 行
    ...

# ... 其他 communication 路由
```

#### 其他路由文件（类似结构）：

- `subnets.py` - Subnet 管理（第 894-1057 行）
- `monitoring.py` - 监控端点（第 1125-1164 行）
- `analytics.py` - 分析端点（第 1166-1223 行）
- `payments.py` - 支付相关（第 1404-1760 行）
- `websocket.py` - WebSocket 端点（第 818-892 行）

### Step 4: 重构主 API 文件（1小时）

**新的 `acn/api/__init__.py`**：

```python
"""ACN API Application"""
import structlog
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from ..config import get_settings
from ..a2a_integration import create_a2a_app
from .routes import registry, communication, subnets, monitoring, analytics, payments, websocket

settings = get_settings()
logger = structlog.get_logger()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    # 启动逻辑
    logger.info("Starting ACN")
    redis = Redis.from_url(settings.redis_url)
    
    # 初始化 A2A
    a2a_app = await create_a2a_app(redis)
    app.mount("/a2a", a2a_app)
    
    app.state.redis = redis
    
    yield
    
    # 关闭逻辑
    logger.info("Shutting down ACN")
    await redis.close()

# 创建 FastAPI app
app = FastAPI(
    title="ACN - Agent Collaboration Network",
    description="Infrastructure for AI agent coordination",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(registry.router)
app.include_router(communication.router)
app.include_router(subnets.router)
app.include_router(monitoring.router)
app.include_router(analytics.router)
app.include_router(payments.router)
app.include_router(websocket.router)

# 根路由和健康检查
@app.get("/")
async def root():
    return {"message": "ACN API", "version": "0.1.0"}

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/.well-known/agent-card.json")
async def get_acn_agent_card():
    """ACN Agent Card"""
    # 保留原有逻辑
    ...
```

### Step 5: 更新导入（30分钟）

**旧代码**：
```python
from acn.api import app  # 导入巨型文件
```

**新代码**：
```python
from acn.api import app  # 导入模块化结构
```

外部接口保持不变！

### Step 6: 测试验证（30分钟）

```bash
# 1. 启动服务
uvicorn acn.api:app --port 8002

# 2. 运行集成测试
pytest tests/

# 3. 运行 Cursor ACN 测试
python agent-adapters/scripts/test_cursor_acn_integration.py

# 4. 验证所有端点
curl http://localhost:8002/health
curl http://localhost:8002/.well-known/agent-card.json
curl http://localhost:8002/api/v1/agents
```

---

## 📊 对比：重构前 vs 重构后

### 重构前
```
acn/
└── api.py (1794 行) ⚠️ 巨型文件
    ├── Registry 路由 (10+ 端点)
    ├── Communication 路由 (8+ 端点)
    ├── Subnets 路由 (6+ 端点)
    ├── Monitoring 路由 (4+ 端点)
    ├── Analytics 路由 (5+ 端点)
    ├── Payments 路由 (8+ 端点)
    └── WebSocket 路由 (3+ 端点)
```

### 重构后
```
acn/api/
├── __init__.py (150 行) ✅ 主应用
├── dependencies.py (100 行) ✅ 共享依赖
└── routes/
    ├── registry.py (250 行) ✅ 清晰
    ├── communication.py (200 行) ✅ 清晰
    ├── subnets.py (180 行) ✅ 清晰
    ├── monitoring.py (100 行) ✅ 清晰
    ├── analytics.py (120 行) ✅ 清晰
    ├── payments.py (300 行) ✅ 清晰
    └── websocket.py (100 行) ✅ 清晰
```

---

## ✅ 收益

### 1. **开发效率提升 50%**
- 只需打开相关的路由文件（200-300行）
- 而不是在 1794 行中搜索

### 2. **Git 协作更顺畅**
- 多人可以同时修改不同路由模块
- 减少冲突

### 3. **测试更容易**
- 每个路由模块可以独立测试
- Mock 依赖更简单

### 4. **新功能添加更快**
- 在对应的 routes 文件中添加
- 结构清晰，不会遗漏

---

## ⏱️ 时间估算

| 步骤 | 预计时间 | 难度 |
|------|---------|------|
| Step 1: 创建目录 | 5分钟 | ⭐ |
| Step 2: 提取依赖 | 30分钟 | ⭐⭐ |
| Step 3: 拆分路由 | 2小时 | ⭐⭐⭐ |
| Step 4: 重构主文件 | 1小时 | ⭐⭐ |
| Step 5: 更新导入 | 30分钟 | ⭐ |
| Step 6: 测试验证 | 30分钟 | ⭐⭐ |
| **总计** | **4.5小时** | |

---

## 🚀 开始执行？

我可以立即开始重构，或者你可以：
1. **立即执行** - 我帮你完成重构
2. **稍后执行** - 先推送当前的审核报告
3. **部分执行** - 只重构最重要的部分（如 registry + communication）

你想怎么做？


