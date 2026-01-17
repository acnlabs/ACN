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

from contextlib import asynccontextmanager

import structlog  # type: ignore[import-untyped]
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .infrastructure.messaging import (
    BroadcastService,
    MessageRouter,
    SubnetManager,
    WebSocketManager,
)
from .infrastructure.persistence.redis import RedisAgentRepository, RedisSubnetRepository
from .infrastructure.persistence.redis.registry import AgentRegistry
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
    payments,
    registry,
    subnets,
    websocket,
)
from .services import AgentService, BillingService, MessageService, SubnetService

# Settings
settings = get_settings()
logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager"""
    logger.info("acn_starting", version="0.1.0")

    # Initialize core services
    registry_instance = AgentRegistry(settings.redis_url)

    # Initialize Clean Architecture services
    agent_repository = RedisAgentRepository(registry_instance.redis)
    agent_service_instance = AgentService(agent_repository)

    subnet_repository = RedisSubnetRepository(registry_instance.redis)
    subnet_service_instance = SubnetService(subnet_repository)

    router_instance = MessageRouter(registry_instance, registry_instance.redis)
    message_service_instance = MessageService(router_instance, agent_repository)
    broadcast_instance = BroadcastService(router_instance, registry_instance.redis)
    ws_manager_instance = WebSocketManager(registry_instance.redis)
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
        webhook_url=settings.billing_webhook_url if hasattr(settings, 'billing_webhook_url') else None,
    )

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
        a2a_app = await create_a2a_app(
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

    yield

    # Cleanup
    logger.info("acn_stopping")
    await registry_instance.redis.close()
    logger.info("acn_stopped")


# Create FastAPI app
app = FastAPI(
    title="ACN - Agent Collaboration Network",
    description="Infrastructure for AI agent coordination and communication",
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

# Include routers
app.include_router(registry.router)
app.include_router(communication.router)
app.include_router(subnets.router)
app.include_router(monitoring.router)
app.include_router(analytics.router)
app.include_router(payments.router)
app.include_router(websocket.router)


# Root endpoints
@app.get("/")
async def root():
    """API root"""
    return {
        "name": "ACN - Agent Collaboration Network",
        "version": "0.1.0",
        "docs": "/docs",
        "agent_card": "/.well-known/agent-card.json",
    }


@app.get("/health")
async def health():
    """Health check"""
    return {"status": "healthy"}


@app.get("/.well-known/agent-card.json")
async def get_acn_agent_card():
    """ACN Agent Card (A2A Protocol)"""
    try:
        # Base card structure
        card = {
            "name": "ACN",
            "description": "Agent Collaboration Network - Infrastructure for AI agent coordination",
            "protocolVersion": "0.4.0",
            "url": settings.gateway_base_url,
            "capabilities": {
                "streaming": False,
                "batchProcessing": False,
            },
            "defaultInputModes": ["text/plain", "application/json"],
            "defaultOutputModes": ["text/plain", "application/json"],
            "version": "0.1.0",
            "skills": [
                {
                    "name": "agent_discovery",
                    "description": "Discover and search for agents by skill, status, owner, or name",
                    "tags": ["discovery", "search", "registry"],
                },
                {
                    "name": "message_broadcast",
                    "description": "Broadcast messages to multiple agents using various strategies",
                    "tags": ["broadcast", "communication", "messaging"],
                },
                {
                    "name": "message_routing",
                    "description": "Route messages to specific agents with priority support",
                    "tags": ["routing", "messaging", "direct"],
                },
                {
                    "name": "subnet_management",
                    "description": "Create and manage agent subnets for organized collaboration",
                    "tags": ["subnets", "organization", "groups"],
                },
            ],
        }

        # Add auth if configured
        if hasattr(settings, "auth0_domain") and settings.auth0_domain:
            card["auth"] = {
                "type": "oauth2",
                "oauth2": {
                    "authorizationUrl": f"{settings.auth0_domain}/authorize",
                    "tokenUrl": f"{settings.auth0_domain}/oauth/token",
                    "scopes": {
                        "acn:read": "Read agent information and search agents",
                        "acn:write": "Register and update agents",
                        "acn:admin": "Full administrative access",
                    },
                },
            }

        return card

    except Exception as e:
        logger.error("agent_card_error", error=str(e))
        raise HTTPException(
            status_code=500, detail=f"Failed to generate agent card: {str(e)}"
        ) from e
