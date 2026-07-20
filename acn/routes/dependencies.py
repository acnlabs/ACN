"""FastAPI Dependencies for ACN

Provides dependency injection for core services.
"""

import re
import secrets
import time
from typing import Annotated

from fastapi import BackgroundTasks, Depends, Header, HTTPException, Path, Request
from slowapi import Limiter  # type: ignore[import-untyped]
from slowapi.util import get_remote_address  # type: ignore[import-untyped]

from ..auth.middleware import get_subject
from ..config import get_settings
from ..core.errors import ACNHTTPError, ErrorCode
from ..core.interfaces.escrow_provider import IEscrowProvider
from ..infrastructure.messaging import (
    BroadcastService,
    MessageRouter,
    SubnetManager,
    WebSocketManager,
)
from ..monitoring import (
    Analytics,
    AuditLogger,
    MetricsCollector,
)
from ..monitoring import (
    record_auth_failure as _audit_record_auth_failure,
)
from ..monitoring import (
    set_audit_singleton as _set_audit_singleton,
)
from ..protocols.ap2 import PaymentDiscoveryService, PaymentTaskManager, WebhookService
from ..services import (
    AgentService,
    AllowlistService,
    BillingService,
    FollowService,
    ManifestService,
    MessageService,
    PolicyCheckService,
    SessionService,
    SubnetService,
)
from ..services.activity_service import ActivityService
from ..services.agent_service import hash_api_key
from ..services.join_flow_service import JoinFlowService
from ..services.org_service import OrgService
from ..services.reputation_query_service import ReputationQueryService
from ..services.reputation_service import ReputationService

settings = get_settings()


# ---------------------------------------------------------------------------
# Path-parameter length caps (P2-#3 / H6 follow-up)
# ---------------------------------------------------------------------------
#
# H6 fenced off body-side abuse with a 1 MiB cap + per-string ``max_length``
# on every Pydantic field. Path/query parameters were left unbounded because
# Starlette's URL parser caps headers at ~64 KB anyway — but that ceiling
# only stops the request *before* it hits the ASGI body middleware. A 60 KB
# ``slug`` still flows downstream into:
#
#   - Redis key composition (``acn:subnets:{slug}`` etc.) —— cardinality
#     pressure on the keyspace, harder OPS-side cleanup.
#   - SQL ``WHERE`` clauses — PostgreSQL accepts arbitrary VARCHAR but
#     planner cost climbs with parameter size.
#   - Audit log structured fields — bloats the daily/type lists with
#     50 KB strings each event.
#
# The caps below are conservative ceilings well above any legitimate id
# (typical ACN ids are ``acn:<UUID4>`` ≈ 41 chars; subnet ids are short
# slugs). Numbers chosen to align with the Postgres VARCHAR widths in the
# schema where present, otherwise sized at ~3× typical id length so a
# legacy/exotic id format doesn't regress.
MAX_SUBNET_ID_LEN: int = 100  # matches Postgres String(100) on tasks.slug
MAX_AGENT_ID_LEN: int = 128
MAX_TASK_ID_LEN: int = 128
MAX_PARTICIPATION_ID_LEN: int = 128
# ADR-0004 §SubnetJoinRequest schema fixes ``request_id`` at UUID4
# shape (36 chars). Cap at 64 chars for forward compatibility with
# any future longer encoding (matches the AGENT_ID cap pattern).
MAX_REQUEST_ID_LEN: int = 64

SubnetIdPath = Annotated[
    str,
    Path(max_length=MAX_SUBNET_ID_LEN, description="Subnet identifier"),
]
AgentIdPath = Annotated[
    str,
    Path(max_length=MAX_AGENT_ID_LEN, description="Agent identifier"),
]
TaskIdPath = Annotated[
    str,
    Path(max_length=MAX_TASK_ID_LEN, description="Task identifier"),
]
ParticipationIdPath = Annotated[
    str,
    Path(max_length=MAX_PARTICIPATION_ID_LEN, description="Participation identifier"),
]
RequestIdPath = Annotated[
    str,
    Path(
        max_length=MAX_REQUEST_ID_LEN,
        description="SubnetJoinRequest identifier (join-request or invitation row id)",
    ),
]


def _get_real_ip(request: Request) -> str:
    """Extract real client IP.

    XFF / X-Real-IP are honoured ONLY when the immediate TCP peer is in
    ``settings.trusted_proxies``. Without this guard any client could
    spoof ``X-Forwarded-For`` to bypass per-IP rate limits (C1a finding
    in the pre-launch security audit).

    When ``trusted_proxies`` is empty we always use the direct peer IP.
    """
    direct_ip = get_remote_address(request)
    trusted = set(settings.trusted_proxies or [])
    if trusted and direct_ip in trusted:
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip
    return direct_ip


def _record_auth_failure(
    *,
    reason: str,
    request: Request | None = None,
    actor_id: str | None = None,
    slug: str = "",
    extra: dict | None = None,
) -> None:
    """Proxy-aware wrapper around ``monitoring.record_auth_failure``.

    Adds three things on top of the base helper:
      - extracts ``source_ip`` via ``_get_real_ip`` (honours
        ``trusted_proxies`` like the rate limiter does, so auth-failure
        attribution matches per-IP throttling for the same client).
      - extracts request ``path`` + ``method`` so analysts can identify
        which endpoint was being probed without cross-referencing logs.
      - never raises on diagnostic failures.
    """
    source_ip: str | None = None
    path: str | None = None
    method: str | None = None
    if request is not None:
        try:
            source_ip = _get_real_ip(request)
        except Exception:  # noqa: BLE001 — never break an auth path on diagnostics
            source_ip = None
        try:
            path = request.url.path
            method = request.method
        except Exception:  # noqa: BLE001
            pass
    _audit_record_auth_failure(
        reason=reason,
        source_ip=source_ip,
        actor_id=actor_id,
        path=path,
        method=method,
        slug=slug,
        extra=extra,
    )


def _rate_limit_key(request: Request) -> str:
    """Rate-limit key.

    Authenticated requests (where an upstream dependency has set
    ``request.state.rate_limit_key``) are bucketed per-agent so a single
    misbehaving agent cannot exhaust the IP-shared budget — this is also
    what makes per-agent abuse easy to attribute and ban.

    Unauthenticated requests fall back to client IP (with XFF only honoured
    via ``_get_real_ip`` for trusted proxies).
    """
    custom_key = getattr(request.state, "rate_limit_key", None)
    if custom_key:
        return custom_key
    return f"ip:{_get_real_ip(request)}"


# ---------------------------------------------------------------------------
# L418 — wallet-dimension rate limiting (Phase 1 P2)
# ---------------------------------------------------------------------------
#
# Why a SECOND bucket on top of the per-agent ``_rate_limit_key``:
#   The per-agent bucket caps any single ``agent_id`` at ``WALLET_RATE / 60``
#   QPS, but agent IDs are cheap — anyone can register an unlimited number
#   of agents under a single wallet. Without a wallet ceiling, the
#   effective abuse rate is ``N_agents × per-agent-rate``, defeating
#   the intent of rate limiting entirely.
#
# Design choice: dual bucket (Scheme B), not fallback (Scheme A).
#   Both ``@limiter.limit(AGENT_RATE)`` and
#   ``@limiter.limit(WALLET_RATE, key_func=_wallet_rate_limit_key)`` are
#   stacked on protected routes. SlowAPI evaluates them independently;
#   either bucket exhausting yields 429. This means a wallet with one
#   noisy agent gets throttled per-agent first (good attribution),
#   while a wallet fan-out across many agents gets throttled per-wallet
#   (good abuse prevention).
#
# Why we don't fall back to ``agent:<id>`` when wallet is missing:
#   Falling back would let attackers opt out of the wallet ceiling
#   simply by not binding a wallet. Instead, ALL un-walleted agents
#   share ONE global ``wallet:none`` bucket. This is intentional:
#   un-walleted agents are "free-tier" identities; they collectively
#   get the same budget as a single walleted owner, so any meaningful
#   throughput needs a wallet binding (which carries gas cost and is
#   therefore harder to spam).
#
# What about the registration / pre-auth path:
#   This key_func runs only on routes that already authenticated via
#   ``verify_agent_api_key`` / ``verify_proxy_caller`` (which is when
#   ``request.state.wallet_address`` gets set). Pre-auth abuse on
#   ``POST /agents/register`` is a separate concern (sign-up rate
#   limit) that Phase 2 covers, not L418.
def _wallet_rate_limit_key(request: Request) -> str:
    """Rate-limit key for the secondary per-wallet bucket.

    Returns ``wallet:<address>`` for walleted agents and ``wallet:none``
    for un-walleted agents (sharing a global free-tier ceiling — see
    the section comment above for why a fallback to per-agent or per-IP
    would defeat the protection).

    Lower-cases the address to avoid splitting the bucket on
    case-variant copies of the same EVM address (``0xAbc`` vs ``0xabc``
    would otherwise count as two distinct buckets).
    """
    wallet = getattr(request.state, "wallet_address", None)
    if not wallet:
        return "wallet:none"
    return f"wallet:{wallet.lower()}"


# Shared rate limiter — backed by Redis for consistency across multiple instances
limiter = Limiter(key_func=_rate_limit_key, storage_uri=settings.redis_url)


# L418: per-wallet ceiling applied as a SECOND ``@limiter.limit`` on
# protected inbound routes (see _wallet_rate_limit_key docstring above).
#
# Sizing rationale (600/minute = 10 req/s):
#   * Legitimate single-wallet operators typically run 1–3 agents at
#     ~60 req/min each (the existing per-agent limit on /send), giving
#     a real-world ceiling of ~60–180 req/min. 600 leaves ~3–10x
#     headroom so honest fan-out never hits this limit.
#   * Abuse scenario: an attacker spinning up 50 agents under one
#     wallet and saturating each at 60 req/min would otherwise emit
#     3 000 req/min. 600/min caps that at 5x the per-agent budget —
#     enough headroom for legitimate scaling, while preventing the
#     "register many agents" exploit from increasing throughput.
#   * Chosen as a round multiple of 60/min so per-agent and per-wallet
#     budgets are easy to reason about together (1 walleted agent uses
#     up to 10 % of its wallet bucket; 10 agents would saturate it).
#
# Kept as a module-level constant rather than a settings field for
# Phase 1: tuning belongs in code review, not runtime config, until we
# have telemetry showing legitimate wallets routinely brushing the
# limit. Phase 2 promotes this to ``Settings.wallet_rate_limit`` once
# observability for "wallet bucket utilization" is wired up.
WALLET_RATE_LIMIT = "600/minute"

# Global service instances (initialized in lifespan)
_agent_service: AgentService | None = None
_message_service: MessageService | None = None
_subnet_service: SubnetService | None = None
_router: MessageRouter | None = None
_broadcast: BroadcastService | None = None
_ws_manager: WebSocketManager | None = None
_subnet_manager: SubnetManager | None = None
_metrics: MetricsCollector | None = None
_audit: AuditLogger | None = None
_analytics: Analytics | None = None
_payment_discovery: PaymentDiscoveryService | None = None
_payment_tasks: PaymentTaskManager | None = None
_webhook_service: WebhookService | None = None
_billing_service: BillingService | None = None
_activity_service: ActivityService | None = None
_follow_service: FollowService | None = None
# Phase 1 communication_policy gateway. Shared with MessageRouter and
# SubnetManager via lifespan wiring (see acn/api.py); routes layer holds
# its own reference so the proxy paths in routes/registry.py — which
# bypass MessageRouter — can still apply the gate. Without this dep the
# four reverse-proxy endpoints (POST/PUT/PATCH /{agent_id} and the
# /{agent_id}/{rest_path} catch-all) become a structural bypass of
# communication_policy.
_policy_service: PolicyCheckService | None = None
# Phase 2 PR #1 manifest queue service. Owned by lifespan wiring; the
# manifest routes (routes/manifest.py) and MessageRouter (for
# decision.route_to == "manifest") are the only readers. Defaulting to
# ``None`` lets test harnesses bring the app up without manifest
# wiring — same pattern as ``_policy_service`` above.
_manifest_service: ManifestService | None = None
# Phase 2 PR #2 allowlist service. Owns the dual-layer (PG + Redis)
# trust list backing ``communication_policy.mode=allowlist``. ``None``
# default keeps tests / CLI tools that don't depend on allowlist mode
# operational without spinning up the storage layer.
_allowlist_service: AllowlistService | None = None
# Phase 3 attention_fee — escrow provider used by the manifest ack
# endpoint to release locked funds and (eventually) by a TTL-refund
# worker to return unread fees to senders. ``None`` is supported so
# the legacy / tests-only bring-ups that disable ESCROW_ENABLED keep
# working — the ack endpoint surfaces 503 in that mode rather than
# crashing.
_escrow_provider: IEscrowProvider | None = None
# Phase 3 Session layer. ``None`` default keeps environments that do
# not wire Redis-backed sessions operational.
_session_service: SessionService | None = None
# Saga v0.1 off-chain reputation.
#
# ``_reputation_service`` (writes) is None in Redis-only deployments —
# the routes that need it surface 503 (same degradation pattern as
# ``_policy_service``).
#
# ``_reputation_query_service`` (reads) is *always* installed because
# the query service supports a None repository internally (returns
# zero-filled off-chain counts) and can still serve the chain-only
# projection when the agent has a bound token. Review fix R3 lifted
# the per-request fallback construction up to lifespan time.
_reputation_service: ReputationService | None = None
_reputation_query_service: ReputationQueryService | None = None
# ADR-0004 Slice 2.3 join flow. Composes SubnetService + the two
# admission-related repositories; routes layer holds it so the 14
# new admission endpoints (POST /agents/{a}/subnets/{s} entry,
# allowlist / join_request / invitation verbs) can dispatch the
# six-branch decision tree without re-wiring on every request.
# ``None`` default mirrors the SubnetService pattern: legacy test
# fixtures that don't exercise admission can still bring the app up.
_join_flow_service: JoinFlowService | None = None
_org_service: OrgService | None = None


def init_services(
    agent_service: AgentService,
    message_service: MessageService,
    subnet_service: SubnetService,
    router: MessageRouter,
    broadcast: BroadcastService,
    ws_manager: WebSocketManager,
    subnet_manager: SubnetManager,
    metrics: MetricsCollector,
    audit: AuditLogger,
    analytics: Analytics,
    payment_discovery: PaymentDiscoveryService,
    payment_tasks: PaymentTaskManager,
    webhook_service: WebhookService,
    billing_service: BillingService | None = None,
    activity_service: ActivityService | None = None,
    follow_service: FollowService | None = None,
    policy_service: PolicyCheckService | None = None,
    manifest_service: ManifestService | None = None,
    allowlist_service: AllowlistService | None = None,
    escrow_provider: IEscrowProvider | None = None,
    session_service: SessionService | None = None,
    reputation_service: ReputationService | None = None,
    reputation_query_service: ReputationQueryService | None = None,
    join_flow_service: JoinFlowService | None = None,
    org_service: OrgService | None = None,
) -> None:
    """Initialize global service instances (called from lifespan)"""
    global \
        _agent_service, \
        _message_service, \
        _subnet_service, \
        _router, \
        _broadcast, \
        _ws_manager, \
        _subnet_manager
    global _metrics, _audit, _analytics
    global _payment_discovery, _payment_tasks, _webhook_service, _billing_service
    global _activity_service, _follow_service, _policy_service, _manifest_service
    global _allowlist_service, _escrow_provider, _session_service
    global _reputation_service, _reputation_query_service
    global _join_flow_service
    global _org_service

    _agent_service = agent_service
    _message_service = message_service
    _subnet_service = subnet_service
    _router = router
    _broadcast = broadcast
    _ws_manager = ws_manager
    _subnet_manager = subnet_manager
    _metrics = metrics
    _audit = audit
    _set_audit_singleton(audit)
    _analytics = analytics
    _payment_discovery = payment_discovery
    _payment_tasks = payment_tasks
    _webhook_service = webhook_service
    _billing_service = billing_service
    _activity_service = activity_service
    _follow_service = follow_service
    _policy_service = policy_service
    _manifest_service = manifest_service
    _allowlist_service = allowlist_service
    _escrow_provider = escrow_provider
    _session_service = session_service
    _reputation_service = reputation_service
    _reputation_query_service = reputation_query_service
    _join_flow_service = join_flow_service
    _org_service = org_service


# Dependency functions
def get_agent_service() -> AgentService:
    """Get AgentService instance"""
    if _agent_service is None:
        raise RuntimeError("AgentService not initialized")
    return _agent_service


def get_message_service() -> MessageService:
    """Get MessageService instance"""
    if _message_service is None:
        raise RuntimeError("MessageService not initialized")
    return _message_service


def get_subnet_service() -> SubnetService:
    """Get SubnetService instance"""
    if _subnet_service is None:
        raise RuntimeError("SubnetService not initialized")
    return _subnet_service


def get_router() -> MessageRouter:
    """Get MessageRouter instance"""
    if _router is None:
        raise RuntimeError("MessageRouter not initialized")
    return _router


def get_broadcast() -> BroadcastService:
    """Get BroadcastService instance"""
    if _broadcast is None:
        raise RuntimeError("BroadcastService not initialized")
    return _broadcast


def get_ws_manager() -> WebSocketManager:
    """Get WebSocketManager instance"""
    if _ws_manager is None:
        raise RuntimeError("WebSocketManager not initialized")
    return _ws_manager


def get_subnet_manager() -> SubnetManager:
    """Get SubnetManager instance"""
    if _subnet_manager is None:
        raise RuntimeError("SubnetManager not initialized")
    return _subnet_manager


def get_metrics() -> MetricsCollector:
    """Get MetricsCollector instance"""
    if _metrics is None:
        raise RuntimeError("MetricsCollector not initialized")
    return _metrics


def get_audit() -> AuditLogger:
    """Get AuditLogger instance"""
    if _audit is None:
        raise RuntimeError("AuditLogger not initialized")
    return _audit


def get_analytics() -> Analytics:
    """Get Analytics instance"""
    if _analytics is None:
        raise RuntimeError("Analytics not initialized")
    return _analytics


def get_payment_discovery() -> PaymentDiscoveryService:
    """Get PaymentDiscoveryService instance"""
    if _payment_discovery is None:
        raise RuntimeError("PaymentDiscoveryService not initialized")
    return _payment_discovery


def get_payment_tasks() -> PaymentTaskManager:
    """Get PaymentTaskManager instance"""
    if _payment_tasks is None:
        raise RuntimeError("PaymentTaskManager not initialized")
    return _payment_tasks


def get_webhook_service() -> WebhookService:
    """Get WebhookService instance"""
    if _webhook_service is None:
        raise RuntimeError("WebhookService not initialized")
    return _webhook_service


def get_billing_service() -> BillingService:
    """Get BillingService instance"""
    if _billing_service is None:
        raise RuntimeError("BillingService not initialized")
    return _billing_service


def get_activity_service() -> ActivityService:
    """Get ActivityService instance"""
    if _activity_service is None:
        raise RuntimeError("ActivityService not initialized")
    return _activity_service


def get_follow_service() -> FollowService:
    """Get FollowService instance"""
    if _follow_service is None:
        raise RuntimeError("FollowService not initialized")
    return _follow_service


def get_manifest_service() -> ManifestService:
    """Get the ManifestService instance.

    Phase 2 PR #1 introduces the manifest queue. Like ``get_router``
    and unlike ``get_policy_service``, this raises when uninitialized
    rather than returning ``None`` — the manifest routes have no
    meaningful degraded behaviour without a service to back them, so
    a 500 is preferable to silently no-op'ing requests in a
    half-wired environment.
    """
    if _manifest_service is None:
        raise RuntimeError("ManifestService not initialized")
    return _manifest_service


def get_session_service() -> SessionService:
    """Get the SessionService instance.

    Raises ``RuntimeError`` when uninitialized so misconfigured
    environments surface a loud failure rather than silently no-op'ing
    session requests.
    """
    if _session_service is None:
        raise RuntimeError("SessionService not initialized")
    return _session_service


def get_allowlist_service() -> AllowlistService:
    """Get the AllowlistService instance.

    PR #2 v3 review P1-A3: when the deployment is missing PostgreSQL
    (DATABASE_URL not set), ``api.py`` lifespan logs an
    ``allowlist_service_disabled`` warning and leaves
    ``_allowlist_service`` at None. Calling an allowlist endpoint
    in that mode previously surfaced ``RuntimeError`` → FastAPI 500,
    which falsely tells the client "transient failure, retry".
    The correct semantic is HTTP 503 with a Retry-After hint — the
    feature is *configured-disabled*, not crashed; clients should
    surface the message to operators rather than retry blindly.

    For wired environments (the production lifespan plus most tests)
    this dependency returns the live service exactly as before.
    """
    if _allowlist_service is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Allowlist feature is unavailable — server is missing "
                "PostgreSQL configuration (DATABASE_URL). Contact the "
                "operator to enable allowlist mode."
            ),
            headers={"Retry-After": "300"},
        )
    return _allowlist_service


def get_escrow_provider() -> IEscrowProvider:
    """Get the wired escrow provider, or 503 when escrow is disabled.

    Phase 3 attention_fee endpoints rely on the backend escrow API for
    the lock + release flow. When ``ESCROW_ENABLED=false`` the lifespan
    deliberately skips wiring this dependency — the deployment runs
    without payment settlement (smoke harnesses, dev environments
    without a Backend reachable). Surfaces a 503 with ``Retry-After``
    so the SDK distinguishes "feature disabled" from "transient 5xx".
    """
    if _escrow_provider is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "attention_fee feature is unavailable — server is "
                "running with ESCROW_ENABLED=false. Contact the "
                "operator to enable escrow before retrying."
            ),
            headers={"Retry-After": "300"},
        )
    return _escrow_provider


def get_escrow_provider_optional() -> IEscrowProvider | None:
    """Optional sibling of ``get_escrow_provider``.

    Returns ``None`` instead of raising when the escrow provider is
    not wired. Used by routes that operate happily without an escrow
    backend on the no-fee path but still need to call the provider
    when a fee *is* attached:

    * ``DELETE /communication/manifest/{agent_id}/{mid}`` —
      free-message deletes must keep working under
      ``ESCROW_ENABLED=false``; only the paid-message branch refuses
      to proceed without a provider, in which case the route maps
      ``None`` to a 503 explicitly so callers see the same error
      surface as ``/ack``.

    Exists as a separate dep so ``app.dependency_overrides`` can
    target it independently from the strict ``EscrowProviderDep`` —
    integration tests stub one without disturbing the other.
    """
    return _escrow_provider


def get_policy_service() -> PolicyCheckService | None:
    """Get the PolicyCheckService instance, or ``None`` if policy is not wired.

    Returns ``None`` rather than raising when uninitialized so the four
    reverse-proxy endpoints can degrade gracefully in environments that
    haven't yet adopted Phase 1 (legacy CLI tools, smoke tests bringing
    up partial app state). Production lifespan always installs an
    instance — see the wiring assertion in ``acn/api.py``.
    """
    return _policy_service


def get_reputation_service() -> ReputationService | None:
    """Get the ReputationService instance, or ``None`` in Redis-only deployments.

    Returns ``None`` rather than raising so the write endpoints can
    respond with 503 instead of 500 when reputation is unavailable.
    PostgreSQL is required for v0.1 reputation (``reputation_events``
    table); deployments without PG keep working for everything except
    the reputation routes.
    """
    return _reputation_service


def get_org_service() -> OrgService:
    """Get OrgService instance (Org Harness Kernel)."""
    if _org_service is None:
        raise RuntimeError("OrgService not initialized")
    return _org_service


def get_join_flow_service() -> JoinFlowService:
    """Get the JoinFlowService instance.

    Required for the 14 admission endpoints introduced by ADR-0004
    Slice 2.3 (``POST /agents/{a}/subnets/{s}`` join entry, allowlist /
    join_request / invitation verbs). Raises loudly when not wired —
    production lifespan always installs it; legacy test fixtures that
    bring up the app without admission must stub these routes.
    """
    if _join_flow_service is None:
        raise RuntimeError("JoinFlowService not initialized")
    return _join_flow_service


def get_reputation_query_service() -> ReputationQueryService | None:
    """Get the ReputationQueryService instance.

    Returns ``None`` only when ``init_services`` was not called (test
    fixtures bringing up partial app state). In normal app boot the
    query service is always present — it handles a missing repository
    internally by returning zero-filled off-chain counts, so callers
    don't need a Redis-only branch.
    """
    return _reputation_query_service


# Type aliases for cleaner dependency injection
AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]
MessageServiceDep = Annotated[MessageService, Depends(get_message_service)]
SubnetServiceDep = Annotated[SubnetService, Depends(get_subnet_service)]
RouterDep = Annotated[MessageRouter, Depends(get_router)]
BroadcastDep = Annotated[BroadcastService, Depends(get_broadcast)]
SubnetManagerDep = Annotated[SubnetManager, Depends(get_subnet_manager)]
WsManagerDep = Annotated[WebSocketManager, Depends(get_ws_manager)]
MetricsDep = Annotated[MetricsCollector, Depends(get_metrics)]
AuditDep = Annotated[AuditLogger, Depends(get_audit)]
AnalyticsDep = Annotated[Analytics, Depends(get_analytics)]
PaymentDiscoveryDep = Annotated[PaymentDiscoveryService, Depends(get_payment_discovery)]
PaymentTasksDep = Annotated[PaymentTaskManager, Depends(get_payment_tasks)]
WebhookServiceDep = Annotated[WebhookService, Depends(get_webhook_service)]
BillingServiceDep = Annotated[BillingService, Depends(get_billing_service)]
ActivityServiceDep = Annotated[ActivityService, Depends(get_activity_service)]
FollowServiceDep = Annotated[FollowService, Depends(get_follow_service)]
PolicyServiceDep = Annotated["PolicyCheckService | None", Depends(get_policy_service)]
ManifestServiceDep = Annotated[ManifestService, Depends(get_manifest_service)]
SessionServiceDep = Annotated[SessionService, Depends(get_session_service)]
AllowlistServiceDep = Annotated[AllowlistService, Depends(get_allowlist_service)]
EscrowProviderDep = Annotated[IEscrowProvider, Depends(get_escrow_provider)]
OptionalEscrowProviderDep = Annotated[
    "IEscrowProvider | None", Depends(get_escrow_provider_optional)
]
ReputationServiceDep = Annotated[
    "ReputationService | None", Depends(get_reputation_service)
]
ReputationQueryServiceDep = Annotated[
    "ReputationQueryService | None", Depends(get_reputation_query_service)
]
JoinFlowServiceDep = Annotated[JoinFlowService, Depends(get_join_flow_service)]
OrgServiceDep = Annotated[OrgService, Depends(get_org_service)]

# Auth dependencies
SubjectDep = Annotated[str, Depends(get_subject)]


# ---------------------------------------------------------------------------
# Agent API Key authentication — with in-memory cache to reduce Redis load
# ---------------------------------------------------------------------------

_API_KEY_CACHE_TTL = 60.0  # seconds
_API_KEY_CACHE_MAX = 10_000  # max entries to prevent unbounded growth
# {cache_key: (agent_id, name, wallet_address, expires_at)}
#
# wallet_address is cached alongside (agent_id, name) so the L418 rate
# limiter can derive a per-wallet bucket without a second Redis lookup
# per request. Adding it to the cache row (rather than a separate cache)
# keeps the row atomic — a single TTL eviction can never leave the
# limiter looking at a stale wallet attached to a fresh agent_id.
_api_key_cache: dict[str, tuple[str, str, str | None, float]] = {}
# Reverse index: agent_id → cache_key.  Maintained alongside
# ``_api_key_cache`` so revocation (unregister / bulk-delete) can
# immediately drop the entry without scanning the whole cache (M3).
# An agent can hold at most one live cache entry at a time because
# ACN assigns exactly one API key per agent_id.
_api_key_cache_by_agent: dict[str, str] = {}


def _get_cached_agent(api_key: str) -> dict | None:
    # Cache key is the SHA-256 hash of the raw key — no plaintext in memory
    cache_key = hash_api_key(api_key)
    entry = _api_key_cache.get(cache_key)
    if entry and entry[3] > time.monotonic():
        return {
            "agent_id": entry[0],
            "name": entry[1],
            "wallet_address": entry[2],
        }
    if entry:
        # Entry expired — clean up both structures atomically
        _api_key_cache_by_agent.pop(entry[0], None)
        del _api_key_cache[cache_key]
    return None


def _cache_agent(
    api_key: str,
    agent_id: str,
    name: str,
    wallet_address: str | None = None,
) -> dict:
    # Cache key is the SHA-256 hash of the raw key — no plaintext in memory
    cache_key = hash_api_key(api_key)
    # Evict all expired entries when the cache is full
    if len(_api_key_cache) >= _API_KEY_CACHE_MAX:
        now = time.monotonic()
        expired = [k for k, v in _api_key_cache.items() if v[3] <= now]
        for k in expired:
            old_agent_id = _api_key_cache[k][0]
            _api_key_cache_by_agent.pop(old_agent_id, None)
            del _api_key_cache[k]
    # If this agent_id already has a stale entry under a different key
    # (e.g. key rotation), remove the old entry first to avoid orphans.
    old_cache_key = _api_key_cache_by_agent.get(agent_id)
    if old_cache_key and old_cache_key != cache_key:
        _api_key_cache.pop(old_cache_key, None)
    _api_key_cache[cache_key] = (
        agent_id,
        name,
        wallet_address,
        time.monotonic() + _API_KEY_CACHE_TTL,
    )
    _api_key_cache_by_agent[agent_id] = cache_key
    return {
        "agent_id": agent_id,
        "name": name,
        "wallet_address": wallet_address,
    }


def evict_agent_from_cache(agent_id: str) -> None:
    """Immediately remove an agent's auth cache entry (M3).

    Call this whenever an agent is deleted or its API key is rotated so
    the revoked credential cannot be used for up to the remaining TTL
    window.  Safe to call even when the agent has no cached entry.
    """
    cache_key = _api_key_cache_by_agent.pop(agent_id, None)
    if cache_key:
        _api_key_cache.pop(cache_key, None)


async def _resolve_agent_by_bearer(
    api_key: str,
    agent_service: AgentService,
    request: Request | None = None,
) -> dict:
    """Resolve a Bearer API key to {agent_id, name} with in-memory caching."""
    cached = _get_cached_agent(api_key)
    if cached:
        return cached
    agent = await agent_service.get_agent_by_api_key(api_key)
    if not agent:
        _record_auth_failure(reason="api_key_invalid", request=request)
        raise ACNHTTPError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            status_code=401,
            details={"reason": "invalid_api_key"},
        )
    # Pull wallet_address from the entity. ``Agent.__post_init__`` keeps
    # the legacy single ``wallet_address`` field in sync with the
    # primary entry of ``wallet_addresses``, so reading the legacy
    # field here is correct for both legacy and multi-chain agents.
    #
    # ``getattr`` (not direct attribute access) on purpose: tests
    # frequently stub ``agent_service.get_agent_by_api_key`` with
    # SimpleNamespace / lightweight dataclass mocks that omit the
    # full Agent surface. A direct ``agent.wallet_address`` would
    # AttributeError those out, masking real failures behind a
    # confusing 500 in unrelated tests. None is a valid wallet
    # value (un-walleted agent) and is correctly handled by the
    # L418 key_func, so falling back to None is safe.
    return _cache_agent(
        api_key,
        agent.agent_id,
        agent.name,
        wallet_address=getattr(agent, "wallet_address", None),
    )


def _schedule_alive_renewal(
    background_tasks: BackgroundTasks | None,
    agent_service: AgentService,
    agent_id: str,
) -> None:
    """Implicit heartbeat: schedule an alive-TTL renewal after the response.

    Called from every agent-API-key auth dependency so any authenticated
    request implicitly keeps the agent ``status="online"`` — no separate
    ``POST /heartbeat`` cron required for an agent that is producing
    business traffic.

    ``background_tasks`` is the FastAPI-injected ``BackgroundTasks`` instance
    (kept as a defensive ``None`` only so unit-test callers that construct
    the dependency directly without a ``BackgroundTasks`` can opt out). The
    renewal itself swallows all errors inside ``AgentService.touch_alive``
    so this is strictly fire-and-forget — it never affects the user-facing
    request that scheduled it.
    """
    if background_tasks is None:
        return
    background_tasks.add_task(agent_service.touch_alive, agent_id)


async def verify_agent_api_key(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: str = Header(..., alias="Authorization", description="Bearer <API_KEY>"),
    agent_service: AgentService = Depends(get_agent_service),
) -> dict:
    """Verify an agent's API key and return {agent_id, name}.

    Side effect: writes ``request.state.agent_id`` so the rate limiter can
    bucket per-agent (see ``_rate_limit_key``), and schedules an implicit
    alive-TTL renewal so any authenticated request keeps the agent online
    without requiring a separate explicit ``/heartbeat`` cron (see
    ``_schedule_alive_renewal``).

    Results are cached in-memory for 60 s (max 10 000 entries) to reduce Redis lookups.
    """
    if not authorization.startswith("Bearer "):
        _record_auth_failure(reason="bearer_format_invalid", request=request)
        raise ACNHTTPError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            status_code=401,
            details={"reason": "invalid_authorization_header_format"},
        )
    api_key = authorization[7:]
    agent_info = await _resolve_agent_by_bearer(api_key, agent_service, request=request)
    request.state.agent_id = agent_info["agent_id"]
    request.state.rate_limit_key = f"agent:{agent_info['agent_id']}"
    # L418: expose the agent's bound wallet address to the
    # ``_wallet_rate_limit_key`` key_func so the secondary
    # per-wallet bucket can be derived without a second auth lookup.
    request.state.wallet_address = agent_info.get("wallet_address")
    _schedule_alive_renewal(background_tasks, agent_service, agent_info["agent_id"])
    return agent_info


AgentApiKeyDep = Annotated[dict, Depends(verify_agent_api_key)]


async def verify_proxy_caller(
    request: Request,
    background_tasks: BackgroundTasks,
    x_acn_authorization: str = Header(
        ...,
        alias="X-ACN-Authorization",
        description="Bearer <ACN_API_KEY> for the calling agent. "
        "Kept distinct from `Authorization` so a downstream agent's auth "
        "header can transit unchanged.",
    ),
    agent_service: AgentService = Depends(get_agent_service),
) -> dict:
    """Authenticate the caller of an agent-proxy route.

    Why a dedicated header instead of reusing ``Authorization``:
      The proxy transparently forwards request headers (including
      ``Authorization``) to the target agent's real endpoint. If we used
      ``Authorization`` for ACN auth, we would either (a) leak the caller's
      ACN API key to the downstream agent, or (b) make it impossible for
      the caller to authenticate to the downstream agent independently.
      ``X-ACN-Authorization`` cleanly separates "auth into ACN" from "auth
      into the target agent".

    Side effect: writes ``request.state.agent_id`` so subsequent rate-limit
    decoration is bucketed per-agent (cannot be bypassed by spoofing XFF).
    """
    if not x_acn_authorization.startswith("Bearer "):
        _record_auth_failure(
            reason="x_acn_authorization_format_invalid",
            request=request,
        )
        raise ACNHTTPError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            status_code=401,
            details={"reason": "invalid_authorization_header_format"},
        )
    api_key = x_acn_authorization[7:]
    agent_info = await _resolve_agent_by_bearer(api_key, agent_service, request=request)
    request.state.agent_id = agent_info["agent_id"]
    request.state.rate_limit_key = f"agent:{agent_info['agent_id']}"
    # L418: same wallet exposure as ``verify_agent_api_key`` —
    # proxy paths must enforce the per-wallet ceiling too, otherwise
    # an attacker could shift the entire abuse pattern from
    # ``/communication/send`` (gated) onto ``/agents/{id}/...``
    # proxy traffic (un-gated) and re-acquire the same multi-account
    # leverage L418 is designed to prevent.
    request.state.wallet_address = agent_info.get("wallet_address")
    # Implicit heartbeat: proxy traffic counts as "business activity" for
    # the calling agent, same as direct routes. The downstream (callee) agent
    # gets its own renewal when it handles its own requests.
    _schedule_alive_renewal(background_tasks, agent_service, agent_info["agent_id"])
    return agent_info


ProxyCallerDep = Annotated[dict, Depends(verify_proxy_caller)]


# ---------------------------------------------------------------------------
# Internal service token authentication
# ---------------------------------------------------------------------------


def verify_internal_token(
    request: Request,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
) -> None:
    """Verify X-Internal-Token for ACN-internal / operator endpoints."""
    expected = settings.internal_api_token
    # In a properly-configured deployment ``expected`` is guaranteed non-empty
    # by ``Settings.validate_security_settings``. The defensive check here is
    # for the unlikely case of a settings reload from an inconsistent state —
    # we never want ``compare_digest`` to be called with ``None``.
    if not expected or not secrets.compare_digest(x_internal_token, expected):
        _record_auth_failure(
            reason="internal_token_invalid",
            request=request,
        )
        raise ACNHTTPError(
            ErrorCode.INTERNAL_TOKEN_INVALID,
            status_code=403,
        )


InternalTokenDep = Annotated[None, Depends(verify_internal_token)]


# ---------------------------------------------------------------------------
# Owner-or-internal authorization (Phase 1 L421)
# ---------------------------------------------------------------------------
#
# Some agent-scoped endpoints (e.g. ``GET /agents/{id}/endpoint``,
# ``PATCH /agents/{id}/policy``) reveal or mutate data that must NOT
# be exposed to arbitrary callers — leaking an agent's real backend
# URL defeats every gate ``communication_policy`` enforces, since a
# caller who has the URL can reach the agent without ever entering
# ACN. Concretely, the two acceptable principals are:
#
#   1. The agent itself, proving ownership via ``Authorization:
#      Bearer <its-API-Key>`` — the API key bearer can already
#      change its own metadata, so reading its own endpoint is
#      strictly less privileged.
#   2. ACN-internal / operator tooling, via ``X-Internal-Token``
#      — the same gate already used by internal-only endpoints.
#
# We deliberately *don't* accept Auth0 owner JWTs here in Phase 1:
# Auth0 ownership and agent ownership are distinct concepts (an
# Auth0 user can own multiple agents but should request each
# endpoint via that agent's own API key for least-privilege).
# When Phase 2 adds an explicit owner-of-agent JWT scope, this
# dependency is the natural place to extend.
async def verify_owner_or_internal(
    request: Request,
    agent_id: AgentIdPath,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(None, alias="Authorization"),
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
    agent_service: AgentService = Depends(get_agent_service),
) -> dict:
    """Authorize as either the agent itself (API key) or ACN-internal.

    Returns a dict tagging the caller principal so handlers can
    log/audit which side of the OR matched:

        {"caller_kind": "internal"} | {"caller_kind": "agent",
                                        "agent_id": "<the agent>"}

    The ``agent_id`` Path parameter is bound by FastAPI from the
    same path the host route uses, so this dependency only mounts
    cleanly on routes shaped ``/{agent_id}/...``.

    Raises:
        401: neither credential present, or both are malformed.
        403: API key valid but for a different agent.
    """
    # X-Internal-Token has priority — if present and valid, the
    # caller is platform infrastructure and we don't even need to
    # look up the agent. Constant-time compare so a wrong token
    # cannot be probed via timing.
    if x_internal_token:
        expected = settings.internal_api_token
        if expected and secrets.compare_digest(x_internal_token, expected):
            return {"caller_kind": "internal"}
        # Internal token present but wrong — fail closed instead of
        # falling through to owner-API-key auth: a half-correct
        # internal token is much more likely to be a misconfigured
        # ops tool than an attacker who *also* has a valid owner
        # API key, and conflating the two would mask the misconfig.
        _record_auth_failure(reason="internal_token_invalid", request=request)
        raise ACNHTTPError(
            ErrorCode.INTERNAL_TOKEN_INVALID,
            status_code=403,
        )

    # Fall back to owner-via-API-key.
    if not authorization or not authorization.startswith("Bearer "):
        _record_auth_failure(reason="owner_or_internal_missing_credential", request=request)
        raise ACNHTTPError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            status_code=401,
            details={"reason": "owner_or_internal_credential_required"},
        )
    api_key = authorization[7:]
    agent_info = await _resolve_agent_by_bearer(api_key, agent_service, request=request)
    if agent_info["agent_id"] != agent_id:
        # Don't leak whether the supplied key was valid for some
        # *other* agent — same shape as wrong key for this agent.
        _record_auth_failure(
            reason="owner_api_key_wrong_agent",
            request=request,
        )
        raise ACNHTTPError(
            ErrorCode.API_KEY_AGENT_MISMATCH,
            status_code=403,
            details={
                "path_agent": agent_id,
                "key_agent": agent_info["agent_id"],
            },
        )
    request.state.agent_id = agent_info["agent_id"]
    request.state.rate_limit_key = f"agent:{agent_info['agent_id']}"
    # Implicit heartbeat (owner-via-API-key branch only). Internal-token
    # callers above are platform infra, not an agent producing traffic, so
    # they intentionally do not extend any agent's alive TTL.
    _schedule_alive_renewal(background_tasks, agent_service, agent_info["agent_id"])
    return {"caller_kind": "agent", "agent_id": agent_info["agent_id"]}


OwnerOrInternalDep = Annotated[dict, Depends(verify_owner_or_internal)]


# ---------------------------------------------------------------------------
# System-caller namespace validation
# ---------------------------------------------------------------------------
#
# Internal endpoints (gated by X-Internal-Token) bypass the per-agent
# ``AgentApiKeyDep`` spoofing check (``agent_info.agent_id == body.from_agent``)
# because there is no authenticated agent — the caller is a trusted backend
# service. To keep that bypass safe we **must** restrict what ``from_agent``
# values an internal caller is allowed to assert: otherwise a leaked internal
# token would be game-over for cross-agent trust (attacker could impersonate
# *any* registered agent when sending messages).
#
# We confine internal callers to a reserved namespace ``system:<slug>``.
# Two reasons this is safe:
#   1. ACN's ``register_agent`` flow assigns ``agent_id = str(uuid4())`` —
#      no registration path can ever produce an ``agent_id`` starting with
#      ``system:``, so this namespace cannot collide with a real agent.
#   2. Recipients can identify ``from_agent`` values starting with
#      ``system:`` as "ACN-trusted system caller" and treat them
#      differently from peer agents (e.g. avoid auto-trust prompts).
#
# Slug character set is intentionally restrictive (``[A-Za-z0-9_-]``) and
# length-bounded (1..64) to make accidental exotic values impossible to ship.
_SYSTEM_CALLER_RE = re.compile(r"^system:[A-Za-z0-9_-]{1,64}$")


def assert_system_caller(from_agent: str) -> None:
    """Validate ``from_agent`` is a well-formed system-caller identifier.

    Raises ``HTTPException(422)`` with a precise error so callers learn
    exactly what shape is required (vs. a generic 400). 422 is chosen over
    400 because the request *was* understood — it just violated the
    semantic rule that internal-channel ``from_agent`` must live in the
    reserved ``system:`` namespace.
    """
    if not _SYSTEM_CALLER_RE.match(from_agent):
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            status_code=422,
            details={
                "field": "from_agent",
                "reason": "system_namespace_required",
                "value": from_agent,
            },
        )
