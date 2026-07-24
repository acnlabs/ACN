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
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
import structlog  # type: ignore[import-untyped]
from a2a.compat.v0_3.types import (  # type: ignore[import-untyped]
    AgentCapabilities,
    AgentProvider,
    AgentSkill,
)
from a2a.compat.v0_3.types import (  # type: ignore[import-untyped]
    AgentCard as A2AAgentCard,
)
from a2a.compat.v0_3.types import (  # type: ignore[import-untyped]
    SecurityScheme as A2ASecurityScheme,
)
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from slowapi import _rate_limit_exceeded_handler  # type: ignore[import-untyped]
from slowapi.errors import RateLimitExceeded  # type: ignore[import-untyped]
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import get_settings
from .core.errors import ACNHTTPError, ErrorCode
from .infrastructure.messaging import (
    BroadcastService,
    ManifestDispatcher,
    MessageRouter,
    SubnetManager,
    WebSocketManager,
)
from .infrastructure.persistence.postgres import (
    PostgresActivityRepository,
    PostgresAgentRepository,
    PostgresAllowlistRepository,
    PostgresBillingRepository,
    PostgresOrgRepository,
    PostgresReputationRepository,
    PostgresSettlementOutboxRepository,
    PostgresSubnetAllowlistRepository,
    PostgresSubnetJoinRequestRepository,
    PostgresSubnetRepository,
    PostgresTaskRepository,
    PostgresUnitOfWork,
    get_engine,
    get_session_factory,
)
from .infrastructure.persistence.redis import (
    RedisAgentRepository,
    RedisAllowlistRepository,
    RedisFollowRepository,
    RedisSubnetRepository,
)
from .infrastructure.persistence.redis.org_repository import RedisOrgRepository

# See note in acn/infrastructure/persistence/redis/__init__.py — these
# two are imported via their submodules to keep the package-level
# import graph cycle-free.
from .infrastructure.persistence.redis.subnet_allowlist_repository import (
    RedisSubnetAllowlistRepository,
)
from .infrastructure.persistence.redis.subnet_join_request_repository import (
    RedisSubnetJoinRequestRepository,
)
from .infrastructure.persistence.redis.task_repository import RedisTaskRepository
from .infrastructure.task_pool import TaskPool
from .middleware import BodySizeLimitMiddleware, SecurityHeadersMiddleware
from .monitoring import Analytics, AuditLogger, MetricsCollector
from .protocols.a2a import (
    A2AFromAgentValidationMiddleware,
    create_a2a_app,
)
from .protocols.ap2 import (
    PaymentDiscoveryService,
    PaymentTaskManager,
    WebhookService,
    create_webhook_config_from_settings,
)
from .routes import (
    agent_subnets,
    allowlist,
    analytics,
    ard,
    communication,
    dependencies,
    follows,
    gateway_connect,
    manifest,
    monitoring,
    oauth,
    onchain,
    orgs,
    payments,
    registry,
    sessions,
    subnet_admission,
    subnets,
    tasks,
    websocket,
)
from .routes.dependencies import limiter
from .security import check_tls_config
from .services import (
    AgentService,
    AllowlistService,
    BillingService,
    FollowService,
    ManifestService,
    MessageService,
    PolicyCheckService,
    SessionService,
    SubnetService,
    TaskService,
)
from .services.activity_service import ActivityService
from .services.erc8004_client import ERC8004Client
from .services.escrow_client import AgentPlanetEscrowProvider
from .services.join_flow_service import JoinFlowService
from .services.org_service import OrgService
from .services.reputation_query_service import ReputationQueryService
from .services.reputation_service import ReputationService
from .services.settlement_worker import SettlementWorker
from .services.webhook_join_flow_event_publisher import (
    WebhookJoinFlowEventPublisher,
)

# Settings
settings = get_settings()
logger = structlog.get_logger()


def _redact_db_url(url: str) -> str:
    """Return database URL with password replaced by *** for safe logging."""
    try:
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(url)
        if parsed.password:
            safe_netloc = parsed.netloc.replace(f":{parsed.password}@", ":***@")
            return urlunparse(parsed._replace(netloc=safe_netloc))
    except Exception:
        pass
    return url[:8] + "..."


# Module-level regex for the SQL ``cap`` constant inside the
# ``enforce_agent_allowlist_capacity`` function body. Compiled once
# so the lifespan check is cheap; the function definition is a
# stable string emitted by alembic migration ``f6a7b8c9d0e1``.
# Captures the integer literal — case-insensitive on the keywords,
# tolerant of whitespace variations from ``pg_get_functiondef``.
_ALLOWLIST_CAP_RE = re.compile(
    r"\bcap\s+CONSTANT\s+integer\s*:=\s*(\d+)",
    re.IGNORECASE,
)


async def _verify_allowlist_cap_alignment(
    session_factory: async_sessionmaker,
    python_cap: int,
) -> None:
    """Compare the trigger SQL ``cap`` constant with the Python constant.

    PR #2 v3 P2-A6 — the Phase 2 PR #2 v3 capacity trigger
    (``trg_agent_allowlist_capacity`` installed by alembic migration
    ``f6a7b8c9d0e1``) hard-codes ``cap = 500`` inside its plpgsql body
    while ``acn.services.allowlist_service.MAX_ALLOWLIST_SIZE`` is
    independently set to 500 in Python. Future operators that bump
    one but forget the other would either:

    * raise ``AllowlistCapacityExceededError`` from the service layer
      before the trigger ever fires (Python tighter), or
    * let the service layer accept inserts that the trigger then
      rejects with SQLSTATE 23514 (SQL tighter) — surfacing as
      ``check_violation`` 500s after the API thought the request
      was fine.

    Either is a soft failure mode. Cross-check at startup so the
    drift shows up in lifespan logs the first time the operator
    deploys an environment with the mismatch, instead of being
    discovered by users tripping the capacity edge case.

    The check is intentionally **non-fatal**: if the trigger isn't
    installed (older alembic head), or PG is in a degraded state, or
    the introspection query fails for any reason, we log and move
    on. The trigger itself remains the canonical guard; this is
    purely a drift detector.
    """
    try:
        async with session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT pg_get_functiondef(oid) "
                    "FROM pg_proc "
                    "WHERE proname = 'enforce_agent_allowlist_capacity' "
                    "LIMIT 1"
                )
            )
            row = result.scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 — non-fatal drift check
        logger.warning(
            "allowlist_capacity_drift_check_failed",
            reason=str(exc),
        )
        return

    if row is None:
        logger.info(
            "allowlist_capacity_drift_check_skipped",
            reason=(
                "enforce_agent_allowlist_capacity function not present; "
                "alembic head likely below f6a7b8c9d0e1"
            ),
        )
        return

    match = _ALLOWLIST_CAP_RE.search(row)
    if match is None:
        logger.warning(
            "allowlist_capacity_drift_check_unparseable",
            reason="cap constant not found in trigger body",
        )
        return

    sql_cap = int(match.group(1))
    if sql_cap != python_cap:
        logger.warning(
            "allowlist_capacity_drift",
            sql_cap=sql_cap,
            python_cap=python_cap,
            advice=(
                "trigger SQL and MAX_ALLOWLIST_SIZE disagree — "
                "update both alembic migration f6a7b8c9d0e1 and "
                "acn.services.allowlist_service.MAX_ALLOWLIST_SIZE"
            ),
        )
    else:
        logger.info(
            "allowlist_capacity_aligned",
            cap=sql_cap,
        )


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

    # Shared Redis client (decode_responses=True matches the legacy
    # ``AgentRegistry.__init__`` setting; downstream callers all
    # assume str values, not bytes). Replaces the
    # ``redis_client`` access pattern that previously
    # piggybacked on ``AgentRegistry`` as a redis-client holder —
    # see ``docs/agent-registry-removal.md`` for the 7-commit
    # migration record (commits 1ee1015..4771a1b).
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

    # Initialize Clean Architecture services
    # Switch between PostgreSQL (durable) and Redis (fallback) based on DATABASE_URL
    _pg_engine = None
    _billing_repository = None
    _activity_repository = None
    # Settlement saga v0.1 wiring — only available in PG mode. See
    # acn/docs/_drafts/settlement-saga-design.md and task_service.py
    # ``_saga_enabled`` for the contract.
    _settlement_outbox_repository: PostgresSettlementOutboxRepository | None = None
    _unit_of_work: PostgresUnitOfWork | None = None
    # Saga v0.1 reputation — same conditional shape as the outbox. The
    # ``reputation_events`` table is PG-only; Redis-only deployments
    # surface 503 on the reputation write routes.
    _reputation_repository: PostgresReputationRepository | None = None
    if settings.database_url:
        logger.info("persistence_postgres", database_url=_redact_db_url(settings.database_url))
        _pg_engine = get_engine(settings.database_url)
        _pg_session = get_session_factory(_pg_engine)
        agent_repository = PostgresAgentRepository(_pg_session, redis_client)
        subnet_repository = PostgresSubnetRepository(_pg_session)
        task_repository = PostgresTaskRepository(_pg_session, redis_client)
        org_repository = PostgresOrgRepository(_pg_session)
        _billing_repository = PostgresBillingRepository(_pg_session)
        _activity_repository = PostgresActivityRepository(_pg_session)
        _settlement_outbox_repository = PostgresSettlementOutboxRepository(_pg_session)
        _unit_of_work = PostgresUnitOfWork(_pg_session)
        _reputation_repository = PostgresReputationRepository(_pg_session)
    else:
        logger.info("persistence_redis", reason="DATABASE_URL not set, using Redis fallback")
        agent_repository = RedisAgentRepository(redis_client)
        subnet_repository = RedisSubnetRepository(redis_client)
        task_repository = RedisTaskRepository(redis_client)
        org_repository = RedisOrgRepository(redis_client)

    agent_service_instance = AgentService(agent_repository)
    # ADR-0003: SubnetService now takes an optional task_repository
    # used by ``create_subnet`` to validate ``linked_task_id`` on
    # ``task_scoped`` child subnets (``linked_task_not_found``
    # rejection path). Always supplied in production composition;
    # legacy test fixtures may instantiate without it.
    # ``agent_repository`` is wired so ``SubnetService.delete_subnet``
    # can clear ``agent.subnet_ids`` back-references on every member
    # before the subnet record is removed (issue #56). Without this
    # wiring, agent-side dust accumulates and gets amplified by every
    # parent-delete cascade.
    # ``unit_of_work`` (ADR-0004 Slice 2.1.1 / issue #75) is wired
    # whenever PG mode is on so ``delete_subnet`` can run the
    # three-table cascade (subnet_join_requests + subnet_allowlist +
    # subnets) inside one transaction. In Redis-only mode
    # ``_unit_of_work`` is None and the service falls back to the
    # sequential-commit path, matching ADR §"Cascade deletion: Redis".
    #
    # ADR-0004 Slice 2.2 — wire the two cascade repositories so the
    # ten new join-flow methods (add_allowlist / approve / invite /
    # accept / reject / withdraw / cancel + reads) have real
    # backends. Slice 2.4 will wire the real
    # ``WebhookJoinFlowEventPublisher`` here; until then the service
    # defaults to the in-house :class:`NoOpJoinFlowEventPublisher`
    # so the eight join-flow webhooks are silently dropped at debug
    # log level (call sites stay publisher-aware regardless).
    if settings.database_url:
        subnet_join_request_repository = PostgresSubnetJoinRequestRepository(_pg_session)
        subnet_admission_allowlist_repository = PostgresSubnetAllowlistRepository(_pg_session)
    else:
        subnet_join_request_repository = RedisSubnetJoinRequestRepository(redis_client)
        subnet_admission_allowlist_repository = RedisSubnetAllowlistRepository(redis_client)

    subnet_service_instance = SubnetService(
        subnet_repository,
        task_repository=task_repository,
        agent_repository=agent_repository,
        subnet_join_request_repository=subnet_join_request_repository,
        subnet_allowlist_repository=subnet_admission_allowlist_repository,
        unit_of_work=_unit_of_work,
        # ``join_flow_event_publisher`` deliberately left at default
        # (NoOpJoinFlowEventPublisher). Slice 2.4 will swap in the
        # WebhookService-backed adapter once ``WebhookEventType`` is
        # extended with the eight new join-flow events.
    )

    # ADR-0004 Slice 2.2 — JoinFlowService implements §join's
    # six-branch decision tree (open / owner / invitation_self /
    # invitation_allowlist / allowlist_auto / pending_join_request).
    # Slice 2.3 wires the HTTP routes that call it; until then this
    # instance is held on ``app.state`` so smoke tests and ad-hoc
    # admin tooling can reach it (matches the existing ``limiter``
    # state-stash pattern down below). The Slice 2.3 router-factory
    # call will read it back via ``app.state.join_flow_service`` so
    # this composition root doesn't need re-touching when the routes
    # land.
    join_flow_service_instance = JoinFlowService(
        subnet_service=subnet_service_instance,
        join_request_repository=subnet_join_request_repository,
        allowlist_repository=subnet_admission_allowlist_repository,
        # Default no-op publisher matches subnet_service_instance
        # above; same Slice-2.4 swap point.
    )
    app.state.join_flow_service = join_flow_service_instance

    # Phase 1 communication_policy gateway: a single PolicyCheckService
    # instance is shared by both the HTTP-side MessageRouter and the
    # WebSocket-side SubnetManager so the two paths are guaranteed to
    # apply the same gate (see "Phase 1 网关执行点决策" in
    # docs/features/acn-communication-economic-model.md). Sharing a
    # single instance is intentional — having two would let the policy
    # rules drift if a future caller mutated one of them.
    policy_service_instance = PolicyCheckService()

    # Initialize monitoring (Analytics is wired with activity_service
    # below, after ActivityService is constructed). Hoisted ahead of
    # the router/dispatcher block (Phase 2 PR #1 review fix P0-A1) so
    # the manifest dispatcher can take a metrics handle for the
    # ``messages_diverted_to_manifest_total`` counter.
    metrics_instance = MetricsCollector(redis_client)
    audit_instance = AuditLogger(redis_client)

    # Phase 2 PR #1: ManifestService is owned alongside the policy
    # service. Both are stateless thin wrappers over Redis, so they
    # can share the same redis client and lifespan as the registry.
    # WebSocketManager is constructed before the router so the
    # manifest dispatcher can hold a reference for the
    # ``manifest_notification`` push.
    manifest_service_instance = ManifestService(redis_client)
    session_service_instance = SessionService(redis_client)
    ws_manager_instance = WebSocketManager(
        redis_client,
        max_connections=settings.max_websocket_connections,
    )

    # Phase 3 attention_fee: hoist the escrow client before the
    # manifest dispatcher so the dispatcher can take an escrow
    # provider handle (used by the lock branch of dispatch). Tests
    # that disable ESCROW_ENABLED still get a dispatcher — it just
    # raises AttentionFeeLockError if any caller actually attaches
    # a fee, which is the correct behaviour.
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

    # Phase 2 PR #1 review fix (P0-A1): single ManifestDispatcher
    # shared by both the HTTP/A2A path (MessageRouter) and the
    # subnet WebSocket path (SubnetManager). Centralising here keeps
    # manifest semantics — write to the queue, push WS, count metric
    # — uniform across ingress channels. A drift between the two
    # would otherwise let manifest mode silently bypass on one path
    # while functioning on the other (the exact bug PR #1 review
    # caught on the subnet side).
    manifest_dispatcher_instance = ManifestDispatcher(
        manifest_service=manifest_service_instance,
        ws_manager=ws_manager_instance,
        metrics=metrics_instance,
        escrow_provider=escrow_client_instance,
    )

    # Phase 2 PR #2: AllowlistService — dual-layer (PG + Redis).
    # Built before MessageRouter / SubnetManager so it can be threaded
    # in as the ``is_in_allowlist`` callback for both. The Redis
    # cache repository takes a closure pointing at the PG repo's
    # ``list_target_ids`` so cache misses can rebuild without
    # holding a circular handle. AllowlistService falls back to
    # PG-only when ``redis_repo=None``; we always pass the Redis
    # cache in production for the 30s read-through that keeps the
    # hot inbound check off PG. ``allowlist_service_instance`` may
    # be ``None`` if the PG side isn't available (Redis-only
    # deployments) — the Postgres repo NEEDS a session factory, so
    # without DATABASE_URL we leave allowlist disabled and the
    # policy validator already rejects ``mode=allowlist`` schema
    # validation only when the row arrives, but the runtime path
    # falls back to "divert to manifest" which is the safety
    # default. Documented in PR #2 plan P1-B6.
    allowlist_service_instance: AllowlistService | None = None
    if _pg_engine is not None:
        pg_allowlist_repo = PostgresAllowlistRepository(_pg_session)
        redis_allowlist_repo = RedisAllowlistRepository(
            redis_client,
            pg_loader=pg_allowlist_repo.list_target_ids,
        )
        allowlist_service_instance = AllowlistService(
            pg_repo=pg_allowlist_repo,
            redis_repo=redis_allowlist_repo,
            agent_repository=agent_repository,
        )
        # PR #2 v3 P2-A6: cross-check that the trigger SQL
        # constant in alembic migration f6a7b8c9d0e1 still matches
        # the Python ``MAX_ALLOWLIST_SIZE``. Non-fatal — see the
        # helper docstring for the rationale.
        from .services.allowlist_service import MAX_ALLOWLIST_SIZE

        await _verify_allowlist_cap_alignment(_pg_session, MAX_ALLOWLIST_SIZE)
    else:
        logger.warning(
            "allowlist_service_disabled",
            reason=(
                "DATABASE_URL not set; allowlist trust list requires "
                "PostgreSQL. Allowlist-mode policies will fall back "
                "to 'divert to manifest' on every check."
            ),
        )

    router_instance = MessageRouter(
        agent_service_instance,
        redis_client,
        policy_service=policy_service_instance,
        manifest_dispatcher=manifest_dispatcher_instance,
        allowlist_service=allowlist_service_instance,
        # ADR-0012 Mode B: same WS manager that accepts agent ``acn listen``
        # connections, so MessageRouter can push ACN-mediated A2A messages
        # over the agent's outbound socket in real time.
        ws_manager=ws_manager_instance,
    )
    message_service_instance = MessageService(router_instance, agent_repository)
    # Phase 2 Group C #9: BroadcastService now requires agent_repository
    # because the unified ``broadcast()`` entry point (used by HTTP routes)
    # resolves subnet/tag/all filters via the same Clean-Architecture
    # repository previously hit by ``MessageService.broadcast_message``.
    broadcast_instance = BroadcastService(
        router_instance,
        redis_client,
        agent_repository=agent_repository,
    )
    # Implicit heartbeat: every inbound WS HEARTBEAT frame fires
    # ``AgentService.touch_alive(agent_id)`` as a detached task, so a
    # WS-connected agent that sends nothing but heartbeats still keeps
    # the Redis ``alive`` TTL refreshed — symmetric with the
    # HTTP-side hook in ``routes/dependencies.py``.
    subnet_manager_instance = SubnetManager(
        agent_service=agent_service_instance,
        redis_client=redis_client,
        gateway_base_url=settings.gateway_base_url,
        policy_service=policy_service_instance,
        manifest_dispatcher=manifest_dispatcher_instance,
        allowlist_service=allowlist_service_instance,
    )

    # Initialize payment services
    webhook_config = create_webhook_config_from_settings(settings)
    webhook_service_instance = WebhookService(
        redis_client,
        webhook_config,
        outbox_enabled=settings.webhook_outbox_enabled,
        outbox_poll_interval=settings.webhook_outbox_poll_interval,
        outbox_max_age_seconds=settings.webhook_outbox_max_age_seconds,
        outbox_max_backoff=settings.webhook_outbox_max_backoff,
    )

    # ADR-0004 Slice 2.4 — swap the no-op join-flow publisher used by
    # ``SubnetService`` + ``JoinFlowService`` for a real
    # ``WebhookJoinFlowEventPublisher`` now that ``WebhookService``
    # exists. We use post-construction injection (rather than
    # threading ``webhook_service_instance`` up to the service
    # constructors at L352/L375) for two reasons:
    #
    # 1. Constructor order — moving ``WebhookService`` up would force
    #    ``redis_client`` + ``webhook_config`` setup ahead of every
    #    repository and service that doesn't need them, bloating the
    #    composition root's "build order" surface.
    # 2. Cheap reversibility — Slice 2.4 rollback is a single-line
    #    diff (delete this block), no need to undo signature changes
    #    on the two services.
    #
    # Both services expose ``event_publisher`` / ``_event_publisher``
    # as settable attributes; the no-op default they were constructed
    # with becomes unreachable after this assignment.
    _join_flow_webhook_publisher = WebhookJoinFlowEventPublisher(
        webhook_service=webhook_service_instance,
    )
    subnet_service_instance.event_publisher = _join_flow_webhook_publisher
    join_flow_service_instance._event_publisher = _join_flow_webhook_publisher

    # Org Harness Kernel (ADR-0014) — needs subnet + agent + webhook.
    # task_repository enables Org → Task Pool import (metadata.org_work_id).
    org_service_instance = OrgService(
        org_repository=org_repository,
        subnet_service=subnet_service_instance,
        agent_service=agent_service_instance,
        webhook_service=webhook_service_instance,
        task_repository=task_repository,
    )

    payment_discovery_instance = PaymentDiscoveryService(redis_client)
    # Inject payment_discovery into AgentService so registration auto-syncs the index
    agent_service_instance.payment_discovery = payment_discovery_instance
    # Inject webhook_service so ownership changes (claim/transfer/release) emit
    # ``agent.owner_changed`` to the platform Backend for wallet owner re-pointing.
    agent_service_instance.webhook_service = webhook_service_instance
    payment_tasks_instance = PaymentTaskManager(
        redis=redis_client,
        discovery=payment_discovery_instance,
        webhook_service=webhook_service_instance,
    )

    # Initialize billing service
    billing_service_instance = BillingService(
        redis=redis_client,
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
        redis=redis_client,
        repository=_activity_repository,
    )

    # Initialize Follow Service.
    # Follow data lives in Redis regardless of agent persistence backend
    # (mirrors how heartbeat ``alive`` keys stay in Redis even when
    # agents themselves are stored in PostgreSQL — both are ephemeral
    # social-graph signals that don't need transactional durability).
    follow_repository = RedisFollowRepository(redis_client)
    follow_service_instance = FollowService(
        follow_repository=follow_repository,
        agent_repository=agent_repository,
    )
    # Cross-wire so AgentService can drop follow data on agent deletion.
    agent_service_instance.follow_service = follow_service_instance

    # Analytics is constructed here (after ActivityService) so it can receive
    # activity_service via the constructor rather than a post-hoc attribute set.
    analytics_instance = Analytics(
        redis=redis_client,
        activity_service=activity_service_instance,
        agent_repo=agent_repository,
        subnet_repo=subnet_repository,
    )

    # Escrow client was hoisted to the manifest_dispatcher block
    # above so attention_fee can lock funds before the manifest
    # entry is written. ``escrow_client_instance`` is bound there
    # — reused below for task budget management without
    # constructing a second client.

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
        # ADR-0003 Phase 3 — task-state cascade dissolves task_scoped
        # subnets when the linked task hits a terminal state.
        subnet_service=subnet_service_instance,
        # org-wallet-v0: Org-paid task cancel → treasury principal + escrow refund
        org_service=org_service_instance,
        # Task invite → A2A push (Mode A/B/inbox via MessageRouter)
        message_service=message_service_instance,
        # Settlement saga v0.1 — both are None in Redis-only mode,
        # which forces ``complete_task`` onto its legacy non-atomic
        # path (existing pre-v0.1 behavior). In PG mode the saga
        # path activates whenever ``outbox_enqueue_required`` is also
        # True (default), atomically committing CAS + outbox enqueue.
        settlement_outbox=_settlement_outbox_repository,
        unit_of_work=_unit_of_work,
        outbox_enqueue_required=settings.outbox_enqueue_required,
    )

    # Set task service for routes
    tasks.set_task_service(task_service_instance)

    # Saga v0.1 reputation services.
    #
    # ``ReputationService`` (write path) is PG-only — without a
    # repository there's nowhere to store rows, so it stays None in
    # Redis-only deployments and the POST endpoints surface 503.
    #
    # ``ReputationQueryService`` (read path) supports a None repository
    # by returning zero-filled off-chain counts; we construct it
    # unconditionally so the GET /reputation/summary endpoint can
    # still serve chain-only numbers in Redis-only deployments
    # (review fix R3 — previously the route built a per-request
    # fallback instance, which was wasteful and hid the contract).
    # The ERC-8004 client is attached later in lifespan once it's
    # been pre-warmed (chain_id verification round-trip).
    reputation_service_instance: ReputationService | None = None
    if _reputation_repository is not None:
        reputation_service_instance = ReputationService(_reputation_repository)
    reputation_query_service_instance = ReputationQueryService(
        repository=_reputation_repository,
        erc8004_client=None,
    )

    # Initialize dependencies
    dependencies.init_services(
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
        follow_service=follow_service_instance,
        policy_service=policy_service_instance,
        manifest_service=manifest_service_instance,
        allowlist_service=allowlist_service_instance,
        escrow_provider=escrow_client_instance,
        session_service=session_service_instance,
        reputation_service=reputation_service_instance,
        reputation_query_service=reputation_query_service_instance,
        join_flow_service=join_flow_service_instance,
        org_service=org_service_instance,
    )

    # Phase 1 wiring guard
    # ----------------------------------------------------------------
    # The four reverse-proxy endpoints in routes/registry.py and the
    # A2A protocol handlers in protocols/a2a/server.py both treat
    # ``policy_service is None`` as "no gate" — a deliberate
    # rollout-safety contract that lets unit tests / partial
    # bring-ups skip the policy layer without crashing. The flip
    # side is that a misconfigured production lifespan (e.g. a
    # future refactor that forgets to wire ``policy_service``) would
    # silently fail-open: closed agents would start receiving traffic
    # they explicitly opted out of, with zero error signal.
    #
    # We use ``raise RuntimeError`` rather than ``assert`` here on
    # purpose: ``assert`` bytecode is stripped under
    # ``python -O`` / ``PYTHONOPTIMIZE=1`` (a perfectly reasonable
    # production toggle for performance reasons), which would
    # silently re-introduce the exact fail-open this guard exists
    # to prevent. ``raise`` is unconditional and survives -O.
    #
    # Verbose error message is intentional — the next person to
    # read it will likely be debugging at 3am.
    if dependencies.get_policy_service() is None:
        raise RuntimeError(
            "PolicyCheckService is not wired — production lifespan must "
            "always inject one via init_services(policy_service=...). "
            "If you are intentionally bringing the app up without policy "
            "(unit tests, smoke harness), construct the dependencies "
            "manually instead of going through this lifespan."
        )

    # Mount A2A Protocol - Infrastructure Agent
    try:
        a2a_app = create_a2a_app(
            agent_service=agent_service_instance,
            router=router_instance,
            broadcast=broadcast_instance,
            subnet_manager=subnet_manager_instance,
            redis=redis_client,
            metrics=metrics_instance,
        )

        # Phase 2 PR #2 P0-1: wrap the A2A app in the from_agent
        # validation middleware so ``allowlist`` mode cannot be
        # bypassed by spoofing ``metadata.from_agent``. The closure
        # captures ``agent_service_instance`` directly rather than
        # going through the dependency container — middleware
        # binding happens before ``init_services`` finishes
        # populating the ``get_*`` lookups, and we want this gate
        # active from the first request. See
        # protocols/a2a/auth_middleware.py docstring for the
        # threat model and design rationale.
        async def _a2a_agent_lookup(credential: str) -> str | None:
            # Fast path: opaque acn_* API key (legacy + mint-only end-state).
            if credential.startswith("acn_"):
                agent = await agent_service_instance.get_agent_by_api_key(credential)
                return agent.agent_id if agent is not None else None
            # JWT path: ACN-issued RS256 agent JWT (ADR-0007 D6, issue #156).
            # Peek at the unverified iss claim; if it matches ACN's issuer,
            # verify offline and return the agent_id from sub.
            try:
                from jose import jwt as _jwt

                from .auth.middleware import (
                    _get_acn_effective_issuer,
                    _verify_acn_agent_jwt,
                )
                from .config import get_settings as _get_settings

                _settings = _get_settings()
                _acn_iss = (_get_acn_effective_issuer(_settings) or "").rstrip("/")
                if _acn_iss:
                    _claims = _jwt.get_unverified_claims(credential)
                    _token_iss = (_claims.get("iss") or "").rstrip("/")
                    if _token_iss == _acn_iss:
                        payload = await _verify_acn_agent_jwt(credential, _settings)
                        return payload.get("sub")
            except Exception:  # noqa: BLE001
                pass
            return None

        guarded_a2a_app = A2AFromAgentValidationMiddleware(
            a2a_app,
            agent_lookup=_a2a_agent_lookup,
        )
        app.mount("/a2a", guarded_a2a_app)
        logger.info("a2a_mounted", path="/a2a", from_agent_validation=True)
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

    # Pre-warm the ERC-8004 client at boot when the integration is enabled.
    # Why fail-fast here: chain_id mismatch is a config disaster (wrong RPC
    # URL or wrong chain_id env var); deferring detection to the first bind
    # request makes the symptom "503s buried in a request log somewhere"
    # instead of a clean refuse-to-start failure.  Unreachable RPC, by
    # contrast, is treated as a transient operability concern (we still
    # boot — the runtime check inside the bind endpoint stays the safety
    # net), so an RPC blip on rolling restart can't take the cluster down.
    if settings.erc8004_enabled:
        erc8004_warm = ERC8004Client(
            rpc_url=settings.erc8004_rpc_url,
            identity_contract=settings.erc8004_identity_contract,
            reputation_contract=settings.erc8004_reputation_contract,
            validation_contract=settings.erc8004_validation_contract,
        )
        try:
            matches, actual = await erc8004_warm.verify_chain_id(settings.erc8004_chain_id)
        except Exception as exc:  # noqa: BLE001 — startup must surface RPC errors clearly
            logger.error(
                "erc8004_startup_verify_failed",
                expected=settings.erc8004_chain_id,
                rpc_url=settings.erc8004_rpc_url,
                error=str(exc),
            )
            matches, actual = False, None
        if matches:
            logger.info(
                "erc8004_chain_id_verified",
                chain_id=actual,
                rpc_url=settings.erc8004_rpc_url,
            )
        elif actual is not None:
            logger.error(
                "erc8004_chain_id_mismatch_at_startup",
                expected=settings.erc8004_chain_id,
                actual=actual,
                rpc_url=settings.erc8004_rpc_url,
            )
            raise RuntimeError(
                f"ERC-8004 RPC reports chain_id={actual} but config expects "
                f"{settings.erc8004_chain_id}; refusing to start (set "
                f"ERC8004_ENABLED=false to bypass for emergencies)."
            )
        else:
            logger.warning(
                "erc8004_rpc_unreachable_at_startup",
                expected=settings.erc8004_chain_id,
                rpc_url=settings.erc8004_rpc_url,
                detail="proceeding; bind endpoint will re-verify per request",
            )
        # Hand the (possibly already-warmed) client to the route singleton
        # so subsequent bind requests reuse it — preserves the chain_id
        # cache so binds don't pay an extra RPC roundtrip.
        onchain.set_erc8004_client(erc8004_warm)
        # Same client also feeds the merged reputation summary so the
        # off-chain + on-chain view in v0.1 can return the chain numbers
        # without ever having to construct a second client. None-safe:
        # in Redis-only deployments the query service is None.
        if reputation_query_service_instance is not None:
            reputation_query_service_instance.attach_erc8004_client(erc8004_warm)

    # Activate audit writes for the lifetime of the app.  Without this,
    # ``AuditLogger._started`` stays False and every ``fire_and_forget_event``
    # short-circuits at its first guard — silently turning all H-audit
    # security writes (auth failures, SSRF blocks, admin bulk deletes)
    # into no-ops in production.  ``start()`` itself logs a SYSTEM_STARTED
    # event so we can see the boundary in the audit stream.
    await audit_instance.start()

    # Hydrate SubnetManager from the authoritative PostgreSQL SubnetRepository.
    # Without this, only the hardcoded "public" subnet is visible to the WebSocket
    # gateway at startup, so WS connections to any subnet created via REST API
    # would be rejected with "Subnet not found" after a restart.
    try:
        from .routes.subnets import _subnet_entity_to_info as _entity_to_info

        _all_subnets = await subnet_service_instance.list_subnets()
        _subnet_infos = [_entity_to_info(s) for s in _all_subnets]
        _added = subnet_manager_instance.hydrate_from_subnet_infos(_subnet_infos)
        logger.info("subnet_manager_startup_hydration_done", total=len(_subnet_infos), added=_added)
    except Exception as _hydration_exc:
        logger.warning(
            "subnet_manager_startup_hydration_failed",
            error=str(_hydration_exc),
        )

    logger.info("acn_started")

    # The legacy ``_heartbeat_watchdog`` was removed alongside the
    # alive-as-single-source-of-truth refactor: it existed only to flip
    # ``Agent.status`` from ONLINE to OFFLINE when the Redis ``alive`` key
    # had expired, but the read side no longer consults that column.
    # Redis TTL now provides the same offline detection automatically and
    # in real time — see ``AgentService._filter_by_status`` /
    # ``batch_alive``.

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

    # Background worker: refund locked attention_fee escrows whose
    # manifest TTL has expired without an ack or recipient-delete.
    # Runs every 5 minutes; the first run is intentionally delayed
    # the same interval so startup doesn't collide with a deploy that
    # just restarted mid-scan.
    _refund_worker_task: asyncio.Task[None] | None = None
    if escrow_client_instance is not None:
        from acn.services.manifest_ttl_refund_worker import run_once as _run_refund_once

        async def _manifest_ttl_refund_worker():
            while True:
                await asyncio.sleep(300)
                try:
                    counts = await _run_refund_once(
                        redis_client,
                        escrow_client_instance,
                    )
                    if counts["refunded"] or counts["errors"]:
                        logger.info(
                            "manifest_ttl_refund_worker_ran",
                            **counts,
                        )
                except Exception as e:
                    logger.error("manifest_ttl_refund_worker_error", error=str(e))

        _refund_worker_task = asyncio.create_task(_manifest_ttl_refund_worker())

    # Settlement saga worker (Todo 4 framework / Todo 6 step bodies).
    # Default OFF in code: production envs that have wired the outbox
    # via DATABASE_URL still need to flip SETTLEMENT_WORKER_ENABLED
    # explicitly. The reason this is gated behind two checks (env
    # flag AND repo presence) is so that a misconfigured deployment
    # — e.g. Redis-only mode with the flag accidentally on — fails
    # loudly via the warning log below instead of crashing.
    settlement_worker_instance: SettlementWorker | None = None
    if settings.settlement_worker_enabled:
        if _settlement_outbox_repository is None:
            logger.warning(
                "settlement_worker_disabled_no_outbox",
                reason=(
                    "SETTLEMENT_WORKER_ENABLED=true but no PG outbox "
                    "repository is wired (DATABASE_URL unset?). Worker "
                    "skipped — events will accumulate in the bypass path."
                ),
            )
        else:
            settlement_worker_instance = SettlementWorker(
                outbox=_settlement_outbox_repository,
                escrow_provider=escrow_client_instance,
                reputation_service=reputation_service_instance,
                metrics_collector=metrics_instance,
                poll_interval_sec=settings.settlement_poll_interval_sec,
                batch_size=settings.settlement_batch_size,
                max_attempts=settings.settlement_max_attempts,
                backoff_base_sec=settings.settlement_backoff_base_sec,
                backoff_max_sec=settings.settlement_backoff_max_sec,
                janitor_interval_sec=settings.settlement_janitor_interval_sec,
                janitor_stuck_threshold_sec=settings.settlement_janitor_stuck_threshold_sec,
                dlq_alert_webhook=settings.settlement_dlq_alert_webhook,
            )
            await settlement_worker_instance.start()

    # Settlement reconciler — daily cross-check between
    # ``settlement_outbox`` (state='done') and ``reputation_events``
    # (kind='feedback'). The two MUST match because the saga is the
    # sole writer of settlement side effects; non-zero deltas surface
    # via ``acn_settlement_reconcile_delta`` and are the first signal
    # of saga drift. The job is gated on the same PG outbox
    # prerequisite as the worker because there's no point reconciling
    # if no rows exist.
    reconciler_task: asyncio.Task[None] | None = None
    if (
        settings.settlement_reconciler_enabled
        and _settlement_outbox_repository is not None
        and _reputation_repository is not None
    ):
        from acn.services.settlement_reconciler import SettlementReconciler

        reconciler_instance = SettlementReconciler(
            outbox=_settlement_outbox_repository,
            reputation=_reputation_repository,
            metrics_collector=metrics_instance,
        )
        reconcile_interval = settings.settlement_reconcile_interval_sec

        async def _settlement_reconciler_loop() -> None:
            # Run once on startup so the gauge isn't a stale value
            # from before the restart — operators expect "fresh on
            # last deploy". Then settle into the configured cadence.
            #
            # ``run_with_retry`` (not ``run_once``) handles the
            # short-retry-on-PG-blip case described in
            # settlement-saga-design.md §6.2: a single transient
            # failure shouldn't burn a 24h reconciliation window.
            # Exhausted retries return None and we wait for the
            # next tick rather than hammering the DB further.
            await reconciler_instance.run_with_retry(window_seconds=reconcile_interval)
            while True:
                try:
                    await asyncio.sleep(reconcile_interval)
                except asyncio.CancelledError:
                    return
                await reconciler_instance.run_with_retry(window_seconds=reconcile_interval)

        reconciler_task = asyncio.create_task(_settlement_reconciler_loop())

    yield

    # Cleanup. Order matters:
    #   1. Shut down the WebSocket pubsub listener before closing Redis,
    #      otherwise its blocking `async for` on a closed connection
    #      raises noisy errors during shutdown.
    #   2. Close the webhook httpx client before Redis — it reads config
    #      out of Redis on retry paths, and we don't want an in-flight
    #      retry to fault on a closed client.
    #   3. Close MessageRouter (shuts down A2A httpx clients).
    #   4. Close Redis connection pool.
    #   5. Dispose PG engine last (it's the outermost resource).
    sweeper_task.cancel()
    if _refund_worker_task is not None:
        _refund_worker_task.cancel()
    # Settlement worker: graceful stop with bounded timeout so a
    # stuck step can't hang the lifespan shutdown. ``stop()`` flips
    # the internal stop event, awaits the poll + janitor tasks,
    # and cancels them if they don't return in time.
    if settlement_worker_instance is not None:
        try:
            await settlement_worker_instance.stop(timeout=10.0)
        except Exception as exc:  # noqa: BLE001 — never block teardown
            logger.error("settlement_worker_stop_error", error=str(exc))
    if reconciler_task is not None:
        reconciler_task.cancel()
    logger.info("acn_stopping")
    try:
        await ws_manager_instance.stop()
    except Exception as e:
        logger.error("ws_manager_stop_failed", error=str(e))
    try:
        await webhook_service_instance.stop()
    except Exception as e:
        logger.error("webhook_service_stop_failed", error=str(e))
    # Drain in-flight fire-and-forget audit writes BEFORE Redis closes.
    # Order matters: ``stop()`` flips ``_started=False`` so the helper
    # rejects new events; then drain gives existing ones up to 3 s to
    # finish flushing to the still-open Redis pool.  Any remaining task
    # is silently dropped (audit is best-effort, see helper docstring).
    try:
        await audit_instance.stop()
        from acn.monitoring.audit import drain_pending_audit_tasks

        drained, dropped = await drain_pending_audit_tasks(timeout=3.0)
        if drained or dropped:
            logger.info(
                "audit_drain_complete",
                drained=drained,
                dropped=dropped,
            )
    except Exception as e:
        logger.error("audit_stop_failed", error=str(e))
    await router_instance.close()
    # redis-py 5.0.1+ deprecated Redis.close() in favor of aclose() for the
    # async client (https://github.com/redis/redis-py/pull/2745).
    await redis_client.aclose()
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
async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
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
    # Preserve standardized response headers from the exception
    # (notably ``Retry-After`` on 503) while still scrubbing the
    # response body. Retry-After is informational only — it tells
    # clients "this is a known unavailable state, not a transient
    # crash; back off ~N seconds before retrying" and leaks no
    # internal context.
    response_headers: dict[str, str] = {"X-Request-ID": request_id}
    exc_headers = getattr(exc, "headers", None) or {}
    # HTTP header names are case-insensitive (RFC 9110 §5.1) but a plain
    # dict lookup is not. Iterate so any of ``Retry-After`` /
    # ``retry-after`` / ``RETRY-AFTER`` is honoured — Starlette /
    # Httpx / our own dependencies can produce any of these without
    # warning. PR #2 v3 P2-A7.
    retry_after = next(
        (v for k, v in exc_headers.items() if k.lower() == "retry-after"),
        None,
    )
    if retry_after is not None:
        response_headers["Retry-After"] = retry_after
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error_code": "internal_server_error",
            "message": "An internal error occurred. Please try again later.",
            "details": {},
            "request_id": request_id,
        },
        headers=response_headers,
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
            "error_code": "internal_server_error",
            "message": "An internal error occurred. Please try again later.",
            "details": {},
            "request_id": request_id,
        },
        headers={"X-Request-ID": request_id},
    )


@app.exception_handler(ACNHTTPError)
async def _acn_http_error_handler(request: Request, exc: ACNHTTPError) -> JSONResponse:
    """Translate an ``ACNHTTPError`` into a flat ACN error response.

    Phase 2 review v2 P1 #11 — pilot: communication routes.

    Emits the canonical ``{error_code, message, details, request_id}``
    body. Behavioural notes:

    * **No error-level logging.** ACN's convention is that 4xx
      responses are part of the API contract and *expected* — logging
      every one at error level would flood the log pipeline during
      normal operation (e.g. a misconfigured client retrying with the
      wrong API key). Routes that *do* want to record an interesting
      4xx (policy rejections, audit-worthy events) emit
      ``logger.info`` / ``logger.warning`` at the call site, where
      the relevant context lives. The 5xx handlers above remain at
      ``logger.error`` because 5xx is unexpected.
    * **``X-Request-ID`` is overridden, not merged.** Any caller
      value supplied via ``exc.headers`` is silently replaced by the
      handler-issued UUID, so the header always identifies *this*
      request rather than whatever the route author thought to put
      there. Mirrors the 5xx handler's behaviour and keeps the
      ``X-Request-ID`` ↔ ``body.request_id`` invariant intact.
    * **Caller headers pass through.** Anything *other* than
      ``X-Request-ID`` (e.g. ``Retry-After`` for 429 responses) is
      forwarded verbatim.
    """
    request_id = _new_request_id(request)
    response_headers: dict[str, str] = dict(exc.headers or {})
    response_headers["X-Request-ID"] = request_id
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error_code": exc.code.value,
            "message": exc.message,
            "details": exc.details,
            "request_id": request_id,
        },
        headers=response_headers,
    )


@app.exception_handler(RequestValidationError)
async def _request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Translate Pydantic ``RequestValidationError`` (422) into the flat ACN schema.

    FastAPI's default 422 body is ``{"detail": [{"loc": [...], "msg": "...",
    "type": "..."}]}``, which does not match the ``{error_code, message,
    details, request_id}`` shape used by all migrated routes. This handler
    replaces it while preserving the full Pydantic error list under
    ``details.pydantic_errors`` so SDK clients that need location-precise
    messages can still access them.

    Pydantic v2 ``ValidationError.errors()`` can include non-JSON-serialisable
    objects (e.g. ``ValueError`` instances) inside ``ctx`` dicts.  We
    round-trip through ``json.dumps`` with a ``str`` fallback to sanitise them
    before handing the list to ``JSONResponse``.
    """
    import json as _json

    def _safe(obj: object) -> str:
        return str(obj)

    request_id = _new_request_id(request)
    raw_errors = _json.loads(_json.dumps(exc.errors(), default=_safe))
    return JSONResponse(
        status_code=422,
        content={
            "error_code": ErrorCode.VALIDATION_FAILED.value,
            "message": "Request validation failed.",
            "details": {"pydantic_errors": raw_errors},
            "request_id": request_id,
        },
        headers={"X-Request-ID": request_id},
    )


# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    # credentials (cookies/auth headers) must not be sent to wildcard origins;
    # browsers reject such responses and it is a security misconfiguration.
    allow_credentials="*" not in settings.cors_origins,
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
#
# ORDER MATTERS for the follows router. It shares the
# ``/api/v1/agents`` prefix with ``registry.router`` and owns the
# specific ``/{id}/follows/...`` sub-paths. It MUST be registered
# before ``registry.router`` so the registry's catch-all reverse
# proxy at ``/{agent_id}/{rest_path:path}`` does not greedily swallow
# follow requests and forward them to the agent's real endpoint.
app.include_router(follows.router)
# Phase 2 PR #2: allowlist router shares ``/api/v1/agents`` with
# registry & follows. Same precedence rule as follows — registered
# before ``registry.router`` so the catch-all proxy at
# ``/{agent_id}/{rest_path:path}`` does not greedily forward
# allowlist requests to the agent's real endpoint.
app.include_router(allowlist.router)
# ADR-0004 Slice 2.3 — admission endpoints (allowlist / join_request /
# invitation). Included BEFORE ``registry.router`` so the catch-all
# ``/{agent_id}/{rest_path:path}`` proxy doesn't swallow
# ``GET /api/v1/agents/{agent_id}/subnet-invitations``.
app.include_router(subnet_admission.router)
# Canonical agent-side subnet membership routes
# (`POST/DELETE /api/v1/agents/{id}/subnets/{subnet_id}`,
#  `GET /api/v1/agents/{id}/subnets`).
# MUST be registered before `registry.router` for the same reason
# `follows` and `allowlist` are: registry mounts a catch-all
# `/{agent_id}/{rest_path:path}` for A2A proxy forwarding, which would
# otherwise greedily swallow these requests and demand the proxy's
# `X-ACN-Authorization` header.
app.include_router(agent_subnets.router)
app.include_router(registry.router)
app.include_router(onchain.router)
app.include_router(communication.router)
# Phase 2 PR #1: manifest queue routes share the
# /api/v1/communication prefix with the communication router so the
# client surface looks unified (POST /communication/send and GET
# /communication/manifest/... live next to each other in OpenAPI).
# We include it as a separate router rather than folding into
# communication.py so the manifest-specific imports (ManifestServiceDep)
# stay in their own file.
app.include_router(manifest.router)
app.include_router(sessions.router)
app.include_router(subnets.router)
# Subnet gateway WebSocket (A2A NAT path) — same process as REST API
app.include_router(gateway_connect.router)
app.include_router(monitoring.router)
app.include_router(analytics.router)
app.include_router(payments.router)
app.include_router(tasks.router)  # Task Pool API
app.include_router(orgs.router)  # Org Harness Kernel (ADR-0014)
app.include_router(websocket.router)
# ADR-0007: agent JWT issuance (OAuth2 client_credentials) + JWKS / OIDC
# discovery. ACN mints short-lived agent JWTs that resource servers
# verify offline; the long-lived acn_* key is the client credential.
app.include_router(oauth.router)
# ARD (Agentic Resource Discovery) compatibility layer — root-level
# ``GET /.well-known/ai-catalog.json`` + ``POST /search`` so ARD clients
# can discover ACN agents. Discovery-only adapter over AgentService; does
# not touch any business logic. See routes/ard.py.
app.include_router(ard.router)

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
