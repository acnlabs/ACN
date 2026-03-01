"""
ACN FastAPI Application (Modular Structure)

REST API for Agent Collaboration Network.

Provides:
- Layer 1: Agent registration, discovery, and management
- Layer 2: Message routing, broadcasting, and WebSocket
- Layer 3: Monitoring, metrics, and analytics
- Layer 4: Payment capabilities and task management

Based on A2A Protocol: https://github.com/a2aproject/A2A
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
import structlog  # type: ignore[import-untyped]
from a2a.types import (  # type: ignore[import-untyped]
    AgentCapabilities,
    AgentProvider,
    AgentSkill,
)
from a2a.types import (  # type: ignore[import-untyped]
    AgentCard as A2AAgentCard,
)
from a2a.types import (  # type: ignore[import-untyped]
    SecurityScheme as A2ASecurityScheme,
)
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from slowapi import _rate_limit_exceeded_handler  # type: ignore[import-untyped]
from slowapi.errors import RateLimitExceeded  # type: ignore[import-untyped]

from .config import get_settings
from .infrastructure.messaging import (
    BroadcastService,
    MessageRouter,
    SubnetManager,
    WebSocketManager,
)
from .infrastructure.persistence.postgres import (
    PostgresAgentRepository,
    PostgresSubnetRepository,
    PostgresTaskRepository,
    get_engine,
    get_session_factory,
)
from .infrastructure.persistence.redis import RedisAgentRepository, RedisSubnetRepository
from .infrastructure.persistence.redis.registry import AgentRegistry
from .infrastructure.persistence.redis.task_repository import RedisTaskRepository
from .infrastructure.task_pool import TaskPool
from .monitoring import Analytics, AuditLogger, MetricsCollector
from .protocols.a2a.server import create_a2a_app
from .protocols.ap2 import (
    PaymentDiscoveryService,
    PaymentTaskManager,
    WebhookService,
    create_webhook_config_from_settings,
)
from .routes import (
    analytics,
    communication,
    dependencies,
    monitoring,
    onchain,
    payments,
    registry,
    subnets,
    tasks,
    websocket,
)
from .routes.dependencies import limiter
from .services import AgentService, BillingService, MessageService, SubnetService, TaskService
from .services.activity_service import ActivityService
from .services.auth0_client import Auth0CredentialClient
from .services.escrow_client import EscrowClient

# Settings
settings = get_settings()
logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager"""
    logger.info("acn_starting", version=settings.service_version)

    # Initialize core services
    registry_instance = AgentRegistry(settings.redis_url)

    # Initialize Auth0 Credential Client (for Agent M2M credentials)
    auth0_credential_client = Auth0CredentialClient(
        backend_url=settings.backend_url,
        internal_token=settings.internal_api_token,
    )

    # Initialize Clean Architecture services
    # Switch between PostgreSQL (durable) and Redis (fallback) based on DATABASE_URL
    _pg_engine = None
    if settings.database_url:
        logger.info("persistence_postgres", database_url=settings.database_url[:30] + "...")
        _pg_engine = get_engine(settings.database_url)
        _pg_session = get_session_factory(_pg_engine)
        agent_repository = PostgresAgentRepository(_pg_session, registry_instance.redis)
        subnet_repository = PostgresSubnetRepository(_pg_session)
        task_repository = PostgresTaskRepository(_pg_session, registry_instance.redis)
    else:
        logger.info("persistence_redis", reason="DATABASE_URL not set, using Redis fallback")
        agent_repository = RedisAgentRepository(registry_instance.redis)
        subnet_repository = RedisSubnetRepository(registry_instance.redis)
        task_repository = RedisTaskRepository(registry_instance.redis)

    agent_service_instance = AgentService(
        agent_repository,
        auth0_client=auth0_credential_client,
    )
    subnet_service_instance = SubnetService(subnet_repository)

    router_instance = MessageRouter(registry_instance, registry_instance.redis)
    message_service_instance = MessageService(router_instance, agent_repository)
    broadcast_instance = BroadcastService(router_instance, registry_instance.redis)
    ws_manager_instance = WebSocketManager(
        registry_instance.redis,
        max_connections=settings.max_websocket_connections,
    )
    subnet_manager_instance = SubnetManager(
        registry=registry_instance,
        redis_client=registry_instance.redis,
        gateway_base_url=settings.gateway_base_url,
    )

    # Initialize monitoring
    metrics_instance = MetricsCollector(registry_instance.redis)
    audit_instance = AuditLogger(registry_instance.redis)
    analytics_instance = Analytics(registry_instance.redis)

    # Initialize payment services
    webhook_config = create_webhook_config_from_settings(settings)
    webhook_service_instance = WebhookService(webhook_config)
    payment_discovery_instance = PaymentDiscoveryService(registry_instance.redis)
    payment_tasks_instance = PaymentTaskManager(
        redis=registry_instance.redis,
        discovery=payment_discovery_instance,
        webhook_service=webhook_service_instance,
    )

    # Initialize billing service
    billing_service_instance = BillingService(
        redis=registry_instance.redis,
        agent_service=agent_service_instance,
        webhook_url=settings.billing_webhook_url,
    )

    # Initialize Activity Service
    activity_service_instance = ActivityService(redis=registry_instance.redis)

    # Initialize Escrow Client (for Labs task budget management)
    escrow_client_instance = EscrowClient(
        backend_url=settings.backend_url,
        internal_token=settings.internal_api_token,
    )

    # Initialize Task Pool and Service (task_repository already set above)
    task_pool_instance = TaskPool(task_repository)
    task_service_instance = TaskService(
        repository=task_repository,
        task_pool=task_pool_instance,
        payment_manager=payment_tasks_instance,
        webhook_service=webhook_service_instance,
        activity_service=activity_service_instance,
        escrow_client=escrow_client_instance,
        agent_repository=agent_repository,
    )

    # Set task service for routes
    tasks.set_task_service(task_service_instance)

    # Initialize dependencies
    dependencies.init_services(
        registry=registry_instance,
        agent_service=agent_service_instance,
        message_service=message_service_instance,
        subnet_service=subnet_service_instance,
        router=router_instance,
        broadcast=broadcast_instance,
        ws_manager=ws_manager_instance,
        subnet_manager=subnet_manager_instance,
        metrics=metrics_instance,
        audit=audit_instance,
        analytics=analytics_instance,
        payment_discovery=payment_discovery_instance,
        payment_tasks=payment_tasks_instance,
        webhook_service=webhook_service_instance,
        billing_service=billing_service_instance,
    )

    # Mount A2A Protocol - Infrastructure Agent
    try:
        a2a_app = create_a2a_app(
            registry=registry_instance,
            router=router_instance,
            broadcast=broadcast_instance,
            subnet_manager=subnet_manager_instance,
            redis=registry_instance.redis,
        )
        app.mount("/a2a", a2a_app)
        logger.info("a2a_mounted", path="/a2a")
    except Exception as e:
        logger.error("a2a_mount_failed", error=str(e))

    logger.info("acn_started")

    # Background watchdog: sync status field for stale agents every 30 min
    async def _heartbeat_watchdog():
        while True:
            await asyncio.sleep(1800)
            try:
                count = await agent_repository.mark_offline_stale()
                if count:
                    logger.info("heartbeat_watchdog_ran", marked_offline=count)
            except Exception as e:
                logger.error("heartbeat_watchdog_error", error=str(e))

    watchdog_task = asyncio.create_task(_heartbeat_watchdog())

    yield

    # Cleanup
    watchdog_task.cancel()
    logger.info("acn_stopping")
    await router_instance.close()
    await registry_instance.redis.close()
    if _pg_engine is not None:
        await _pg_engine.dispose()
    logger.info("acn_stopped")


# Create FastAPI app
app = FastAPI(
    title="ACN - Agent Collaboration Network",
    description="Infrastructure for AI agent coordination and communication",
    version=settings.service_version,
    lifespan=lifespan,
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
)

# Rate limiter state and error handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Internal-Token"],
    allow_private_network=False,
)

# Include routers
app.include_router(registry.router)
app.include_router(onchain.router)
app.include_router(communication.router)
app.include_router(subnets.router)
app.include_router(monitoring.router)
app.include_router(analytics.router)
app.include_router(payments.router)
app.include_router(tasks.router)  # Task Pool API
app.include_router(websocket.router)

# Note: onboarding.py removed - functionality migrated to:
# - /api/v1/agents/join (registry.py)
# - /api/v1/agents/me (registry.py)
# - /api/v1/analytics/activities (analytics.py)
# - Rewards handled by Backend /api/rewards/*


# Root endpoints
@app.get("/")
async def root():
    """API root"""
    response = {
        "name": "ACN - Agent Collaboration Network",
        "version": settings.service_version,
        "agent_card": "/.well-known/agent-card.json",
    }
    if settings.enable_docs:
        response["docs"] = "/docs"
    return response


@app.get("/health")
async def health():
    """Liveness probe — returns 200 as long as the process is running.

    Railway (and other orchestrators) use this to decide whether to restart the
    container.  We intentionally do NOT check Redis here: a transient Redis
    outage should NOT cause the container to be killed and restarted in a loop.
    Use GET /ready for a full dependency check.
    """
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "version": settings.service_version,
        },
    )


@app.get("/ready")
async def ready():
    """Readiness probe — verifies that all key dependencies are reachable.

    Returns 200 when the service can handle traffic, 503 when a critical
    dependency (e.g. Redis) is unavailable.  Use this for monitoring/alerting
    but do NOT point the Railway healthcheck at it.
    """
    redis_status = "unknown"
    try:
        r = aioredis.from_url(settings.redis_url, socket_connect_timeout=2)
        await r.ping()
        await r.aclose()
        redis_status = "ok"
    except Exception:
        redis_status = "error"

    overall = "healthy" if redis_status == "ok" else "degraded"
    status_code = 200 if overall == "healthy" else 503

    return JSONResponse(
        status_code=status_code,
        content={
            "status": overall,
            "version": settings.service_version,
            "dependencies": {
                "redis": redis_status,
            },
        },
    )


@app.get("/skill.md", response_class=PlainTextResponse)
async def get_skill_md():
    """Serve the ACN skill file for external agents (agentskills.io format)."""
    skill_path = Path(__file__).parent.parent / "skills" / "acn" / "SKILL.md"

    if skill_path.exists():
        return skill_path.read_text()
    return """---
name: acn
description: Agent Collaboration Network — register, discover, message, and collaborate.
---

# ACN — Agent Collaboration Network

Docs: /docs
Agent Card: /.well-known/agent-card.json
"""


@app.get("/.well-known/agent-card.json")
async def get_acn_agent_card():
    """ACN infrastructure Agent Card (A2A Protocol compliant).

    For per-agent cards use: GET /api/v1/agents/{agent_id}/.well-known/agent-card.json
    """
    try:
        security_schemes = None
        security = None

        if settings.auth0_domain:
            security_schemes = {
                "oauth2": A2ASecurityScheme(
                    type="openIdConnect",
                    openIdConnectUrl=f"{settings.auth0_domain}/.well-known/openid-configuration",
                ),
            }
            security = [{"oauth2": []}]

        card = A2AAgentCard(
            protocol_version=settings.a2a_protocol_version,
            name="ACN",
            version=settings.service_version,
            description="Agent Collaboration Network - Infrastructure for AI agent coordination",
            url=settings.gateway_base_url,
            provider=AgentProvider(
                organization="acnlabs",
                url="https://acnlabs.dev",
            ),
            documentation_url=f"{settings.gateway_base_url}/skill.md",
            capabilities=AgentCapabilities(
                streaming=False,
                push_notifications=False,
                state_transition_history=False,
            ),
            default_input_modes=["text", "application/json"],
            default_output_modes=["text", "application/json"],
            security_schemes=security_schemes,
            security=security,
            skills=[
                AgentSkill(
                    id="acn:discovery",
                    name="Agent Discovery",
                    description="Discover and search for agents by skill, status, owner, or name",
                    tags=["discovery", "search", "registry"],
                    input_modes=["application/json"],
                    output_modes=["application/json"],
                ),
                AgentSkill(
                    id="acn:broadcast",
                    name="Message Broadcasting",
                    description="Broadcast messages to multiple agents using various strategies",
                    tags=["broadcast", "communication", "messaging"],
                    input_modes=["application/json"],
                    output_modes=["application/json"],
                ),
                AgentSkill(
                    id="acn:routing",
                    name="Message Routing",
                    description="Route messages to specific agents with priority support",
                    tags=["routing", "messaging", "direct"],
                    input_modes=["text", "application/json"],
                    output_modes=["text", "application/json"],
                ),
                AgentSkill(
                    id="acn:subnet",
                    name="Subnet Management",
                    description="Create and manage agent subnets for organized collaboration",
                    tags=["subnets", "organization", "groups"],
                    input_modes=["application/json"],
                    output_modes=["application/json"],
                ),
            ],
        )

        return card.model_dump(exclude_none=True)

    except Exception as e:
        logger.error("agent_card_error", error=str(e))
        raise HTTPException(
            status_code=500, detail=f"Failed to generate agent card: {str(e)}"
        ) from e
