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
import uuid
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
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from slowapi import _rate_limit_exceeded_handler  # type: ignore[import-untyped]
from slowapi.errors import RateLimitExceeded  # type: ignore[import-untyped]
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import get_settings
from .infrastructure.messaging import (
    BroadcastService,
    MessageRouter,
    SubnetManager,
    WebSocketManager,
)
from .infrastructure.persistence.postgres import (
    PostgresActivityRepository,
    PostgresAgentRepository,
    PostgresBillingRepository,
    PostgresSubnetRepository,
    PostgresTaskRepository,
    get_engine,
    get_session_factory,
)
from .infrastructure.persistence.redis import RedisAgentRepository, RedisSubnetRepository
from .infrastructure.persistence.redis.registry import AgentRegistry
from .infrastructure.persistence.redis.task_repository import RedisTaskRepository
from .infrastructure.task_pool import TaskPool
from .middleware import BodySizeLimitMiddleware, SecurityHeadersMiddleware
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
from .security import check_tls_config
from .services import AgentService, BillingService, MessageService, SubnetService, TaskService
from .services.activity_service import ActivityService
from .services.auth0_client import Auth0CredentialClient
from .services.escrow_client import AgentPlanetEscrowProvider

# Settings
settings = get_settings()
logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager"""
    logger.info("acn_starting", version=settings.service_version)

    # M13: scan URL settings for plain-HTTP misconfiguration in production.
    # Soft check (warning only, never fails startup) because ACN routinely
    # runs behind a TLS-terminating reverse proxy and we cannot reliably
    # tell intra-cluster service-mesh URLs apart from real cleartext leaks.
    # See ``acn.security.tls_check`` for the rationale.
    check_tls_config(settings, logger)

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
    _billing_repository = None
    _activity_repository = None
    if settings.database_url:
        logger.info("persistence_postgres", database_url=settings.database_url[:30] + "...")
        _pg_engine = get_engine(settings.database_url)
        _pg_session = get_session_factory(_pg_engine)
        agent_repository = PostgresAgentRepository(_pg_session, registry_instance.redis)
        subnet_repository = PostgresSubnetRepository(_pg_session)
        task_repository = PostgresTaskRepository(_pg_session, registry_instance.redis)
        _billing_repository = PostgresBillingRepository(_pg_session)
        _activity_repository = PostgresActivityRepository(_pg_session)
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

    # Initialize monitoring (Analytics is wired with activity_service below,
    # after ActivityService is constructed)
    metrics_instance = MetricsCollector(registry_instance.redis)
    audit_instance = AuditLogger(registry_instance.redis)

    # Initialize payment services
    webhook_config = create_webhook_config_from_settings(settings)
    webhook_service_instance = WebhookService(registry_instance.redis, webhook_config)
    payment_discovery_instance = PaymentDiscoveryService(registry_instance.redis)
    # Inject payment_discovery into AgentService so registration auto-syncs the index
    agent_service_instance.payment_discovery = payment_discovery_instance
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
        repository=_billing_repository,
    )
    if billing_service_instance.storage_mode == "redis_fallback":
        logger.warning(
            "billing_on_redis_fallback",
            detail=(
                "BillingService has no PostgreSQL repository. "
                "Financial data uses ephemeral Redis storage (90-day TTL, capped indexes). "
                "Set DATABASE_URL to enable durable PG billing."
            ),
        )

    # Initialize Activity Service
    activity_service_instance = ActivityService(
        redis=registry_instance.redis,
        repository=_activity_repository,
    )

    # Analytics is constructed here (after ActivityService) so it can receive
    # activity_service via the constructor rather than a post-hoc attribute set.
    analytics_instance = Analytics(
        redis=registry_instance.redis,
        activity_service=activity_service_instance,
        agent_repo=agent_repository,
        subnet_repo=subnet_repository,
    )

    # Initialize Escrow Client (for Labs task budget management)
    # When ESCROW_ENABLED=false, tasks still work but payment settlement is skipped.
    if settings.escrow_enabled:
        escrow_client_instance: AgentPlanetEscrowProvider | None = AgentPlanetEscrowProvider(
            backend_url=settings.backend_url,
            internal_token=settings.internal_api_token,
        )
    else:
        escrow_client_instance = None
        logger.warning(
            "escrow_disabled",
            escrow_enabled=False,
            reason="ESCROW_ENABLED=false — tasks will run without payment settlement",
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
        subnet_repository=subnet_repository,
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
        activity_service=activity_service_instance,
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

    # Bring up background workers that own long-lived resources.
    #
    # Previously neither of these was started. WebSocketManager without
    # start() leaves `self._pubsub = None`, so `subscribe()` silently
    # no-ops its Redis subscription and `_listen_pubsub` never runs —
    # i.e. cross-process WebSocket broadcasts are dropped on the
    # receiving node. Single-instance deployments didn't notice because
    # `broadcast()` still fans out locally via `_broadcast_local()`.
    #
    # WebhookService without start() gets lazy-initialized on first
    # send, but then the httpx.AsyncClient is never aclose()'d on
    # shutdown, leaking the connection pool on every process exit.
    await ws_manager_instance.start()
    await webhook_service_instance.start()

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

    # Background sweeper: force-fail payment tasks stuck in non-terminal
    # statuses for more than 7 days.  Runs every 6 hours so the window
    # between creation and expiry is at most 7 days + 6 hours.
    async def _payment_sweeper():
        while True:
            await asyncio.sleep(6 * 3600)
            try:
                swept = await payment_tasks_instance.sweep_stale_tasks(stale_after_days=7)
                if swept:
                    logger.info("payment_sweeper_ran", swept=swept)
            except Exception as e:
                logger.error("payment_sweeper_error", error=str(e))

    sweeper_task = asyncio.create_task(_payment_sweeper())

    yield

    # Cleanup. Order matters:
    #   1. Stop the heartbeat watchdog first so it can't race a teardown.
    #   2. Shut down the WebSocket pubsub listener before closing Redis,
    #      otherwise its blocking `async for` on a closed connection
    #      raises noisy errors during shutdown.
    #   3. Close the webhook httpx client before Redis — it reads config
    #      out of Redis on retry paths, and we don't want an in-flight
    #      retry to fault on a closed client.
    #   4. Close MessageRouter (shuts down A2A httpx clients).
    #   5. Close Redis connection pool.
    #   6. Dispose PG engine last (it's the outermost resource).
    watchdog_task.cancel()
    sweeper_task.cancel()
    logger.info("acn_stopping")
    try:
        await ws_manager_instance.stop()
    except Exception as e:
        logger.error("ws_manager_stop_failed", error=str(e))
    try:
        await webhook_service_instance.stop()
    except Exception as e:
        logger.error("webhook_service_stop_failed", error=str(e))
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


# ---------------------------------------------------------------------------
# Global exception handlers (security-audit H4)
# ---------------------------------------------------------------------------
# Audit finding H4: many handlers do `raise HTTPException(status_code=500,
# detail=str(e))`, which leaks internal exception messages — sometimes with
# stack-trace-level detail (DB error strings, internal hostnames, library
# tracebacks) — to anonymous callers. We cannot fix this purely call-site by
# call-site, so we install global handlers that:
#
# 1. Log the real exception with full context (request_id, path, method) so
#    operators can still debug from the structured logs.
# 2. Replace the response body with a constant-shape generic error and a
#    request_id the caller can quote in support tickets.
# 3. Apply ONLY to ≥500 responses and to fully-unhandled `Exception`s.
#    4xx keep their detail because the caller is expected to act on those
#    messages ("agent not found", "permission denied", validation errors).
#
# This is layered defence: even if a route author forgets and writes
# `raise HTTPException(500, detail=str(e))`, the handler below will sanitise
# the response before it leaves the process.
def _new_request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    if rid:
        return str(rid)
    rid = str(uuid.uuid4())
    try:
        request.state.request_id = rid
    except Exception:  # pragma: no cover  - state may be readonly on some scopes
        pass
    return rid


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Sanitise 5xx HTTPExceptions; pass 4xx through unchanged."""
    if exc.status_code < 500:
        # 4xx semantics are part of the API contract — keep them verbatim.
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None),
        )
    request_id = _new_request_id(request)
    logger.error(
        "http_5xx_response",
        status_code=exc.status_code,
        detail=str(exc.detail),
        path=request.url.path,
        method=request.method,
        request_id=request_id,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": "internal_server_error",
            "message": "An internal error occurred. Please try again later.",
            "request_id": request_id,
        },
        headers={"X-Request-ID": request_id},
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler: any uncaught exception becomes a generic 500.

    Without this, Starlette's default ServerErrorMiddleware echoes the full
    traceback into the response body when ``debug=True`` and a bare error
    message otherwise — neither is acceptable for a public API. We log the
    exception with ``exc_info`` so traces still land in our log pipeline.
    """
    request_id = _new_request_id(request)
    logger.error(
        "unhandled_exception",
        error=type(exc).__name__,
        detail=str(exc),
        path=request.url.path,
        method=request.method,
        request_id=request_id,
        exc_info=True,
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_server_error",
            "message": "An internal error occurred. Please try again later.",
            "request_id": request_id,
        },
        headers={"X-Request-ID": request_id},
    )

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Internal-Token"],
    allow_private_network=False,
)

# Body size cap (security audit H6).
# Registered AFTER CORSMiddleware so it sits *outside* CORS in the wrap order
# (Starlette wraps middleware bottom-up: the last `add_middleware` call is the
# outermost layer).  Putting BodySizeLimit on the outside means oversized
# requests are rejected before any other layer touches them — including
# CORS preflight bookkeeping — which is exactly what we want.
#
# We hand the middleware ``cors_origins`` so its 413 response can carry the
# right ``Access-Control-Allow-Origin`` echo: without it, browsers see a
# generic CORS error instead of a clean 413 and developers chase phantom
# CORS misconfigs (round-2 audit finding).
app.add_middleware(
    BodySizeLimitMiddleware,
    max_bytes=settings.max_request_body_size,
    cors_allow_origins=settings.cors_origins,
)

# Security response headers (security audit M11).
# Registered LAST so it ends up as the OUTERMOST middleware — that way it
# decorates *every* response, including those generated by the body-size
# cap (413), the rate limiter (429), and the global exception handlers
# (500). Inner middleware get their headers added on top of whatever they
# emitted; we never clobber an intentional override.
#
# HSTS is gated on ``dev_mode``: dev rigs frequently run over plain HTTP
# and shipping HSTS would lock localhost browsers into TLS-only access for
# a year. In production (``dev_mode=False``) we always opt in, regardless
# of whether ``gateway_base_url`` is HTTPS — the operator's TLS terminator
# is the source of truth, and HSTS being present even when ACN itself
# happens to be on plain HTTP behind a TLS-terminating proxy is the
# desired behaviour.
app.add_middleware(
    SecurityHeadersMiddleware,
    hsts=not settings.dev_mode,
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

    ``billing_storage`` is informational only; ``"redis_fallback"`` means
    BillingService has no PostgreSQL repository (financial data is ephemeral).
    This does not affect the HTTP status code — it is a degraded condition,
    not a fatal one, and should be monitored via alerting rules rather than
    causing container restarts.
    """
    redis_status = "unknown"
    try:
        r = aioredis.from_url(settings.redis_url, socket_connect_timeout=2)
        await r.ping()
        await r.aclose()
        redis_status = "ok"
    except Exception:
        redis_status = "error"

    # Billing storage mode — reported for observability, not factored into
    # HTTP status code (redis_fallback is degraded, not an outage).
    try:
        billing_storage = dependencies.get_billing_service().storage_mode
    except Exception:
        billing_storage = "unknown"

    overall = "healthy" if redis_status == "ok" else "degraded"
    status_code = 200 if overall == "healthy" else 503

    return JSONResponse(
        status_code=status_code,
        content={
            "status": overall,
            "version": settings.service_version,
            "dependencies": {
                "redis": redis_status,
                "billing_storage": billing_storage,
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
            tags=[
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
