"""FastAPI Dependencies for ACN

Provides dependency injection for core services.
"""

import re
import secrets
import time
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Path, Request
from slowapi import Limiter  # type: ignore[import-untyped]
from slowapi.util import get_remote_address  # type: ignore[import-untyped]

from ..auth.middleware import get_subject
from ..config import get_settings
from ..infrastructure.messaging import (
    BroadcastService,
    MessageRouter,
    SubnetManager,
    WebSocketManager,
)
from ..infrastructure.persistence.redis.registry import AgentRegistry
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
    BillingService,
    FollowService,
    MessageService,
    PolicyCheckService,
    SubnetService,
)
from ..services.activity_service import ActivityService

settings = get_settings()


# ---------------------------------------------------------------------------
# Path-parameter length caps (P2-#3 / H6 follow-up)
# ---------------------------------------------------------------------------
#
# H6 fenced off body-side abuse with a 1 MiB cap + per-string ``max_length``
# on every Pydantic field. Path/query parameters were left unbounded because
# Starlette's URL parser caps headers at ~64 KB anyway — but that ceiling
# only stops the request *before* it hits the ASGI body middleware. A 60 KB
# ``subnet_id`` still flows downstream into:
#
#   - Redis key composition (``acn:subnet:{subnet_id}`` etc.) —— cardinality
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
MAX_SUBNET_ID_LEN: int = 100  # matches Postgres String(100) on tasks.subnet_id
MAX_AGENT_ID_LEN: int = 128
MAX_TASK_ID_LEN: int = 128
MAX_PARTICIPATION_ID_LEN: int = 128

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
    subnet_id: str = "",
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
        subnet_id=subnet_id,
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


# Shared rate limiter — backed by Redis for consistency across multiple instances
limiter = Limiter(key_func=_rate_limit_key, storage_uri=settings.redis_url)

# Global service instances (initialized in lifespan)
_registry: AgentRegistry | None = None
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


def init_services(
    registry: AgentRegistry,
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
) -> None:
    """Initialize global service instances (called from lifespan)"""
    global \
        _registry, \
        _agent_service, \
        _message_service, \
        _subnet_service, \
        _router, \
        _broadcast, \
        _ws_manager, \
        _subnet_manager
    global _metrics, _audit, _analytics
    global _payment_discovery, _payment_tasks, _webhook_service, _billing_service
    global _activity_service, _follow_service, _policy_service

    _registry = registry
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


# Dependency functions
def get_registry() -> AgentRegistry:
    """Get AgentRegistry instance"""
    if _registry is None:
        raise RuntimeError("AgentRegistry not initialized")
    return _registry


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


def get_policy_service() -> PolicyCheckService | None:
    """Get the PolicyCheckService instance, or ``None`` if policy is not wired.

    Returns ``None`` rather than raising when uninitialized so the four
    reverse-proxy endpoints can degrade gracefully in environments that
    haven't yet adopted Phase 1 (legacy CLI tools, smoke tests bringing
    up partial app state). Production lifespan always installs an
    instance — see the wiring assertion in ``acn/api.py``.
    """
    return _policy_service


# Type aliases for cleaner dependency injection
RegistryDep = Annotated[AgentRegistry, Depends(get_registry)]
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

# Auth dependencies
SubjectDep = Annotated[str, Depends(get_subject)]


# ---------------------------------------------------------------------------
# Agent API Key authentication — with in-memory cache to reduce Redis load
# ---------------------------------------------------------------------------

_API_KEY_CACHE_TTL = 60.0  # seconds
_API_KEY_CACHE_MAX = 10_000  # max entries to prevent unbounded growth
# {api_key: (agent_id, name, expires_at)}
_api_key_cache: dict[str, tuple[str, str, float]] = {}


def _get_cached_agent(api_key: str) -> dict | None:
    entry = _api_key_cache.get(api_key)
    if entry and entry[2] > time.monotonic():
        return {"agent_id": entry[0], "name": entry[1]}
    if entry:
        del _api_key_cache[api_key]
    return None


def _cache_agent(api_key: str, agent_id: str, name: str) -> dict:
    # Evict all expired entries when the cache is full
    if len(_api_key_cache) >= _API_KEY_CACHE_MAX:
        now = time.monotonic()
        expired = [k for k, v in _api_key_cache.items() if v[2] <= now]
        for k in expired:
            del _api_key_cache[k]
    _api_key_cache[api_key] = (agent_id, name, time.monotonic() + _API_KEY_CACHE_TTL)
    return {"agent_id": agent_id, "name": name}


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
        raise HTTPException(status_code=401, detail="Invalid API key")
    return _cache_agent(api_key, agent.agent_id, agent.name)


async def verify_agent_api_key(
    request: Request,
    authorization: str = Header(..., alias="Authorization", description="Bearer <API_KEY>"),
    agent_service: AgentService = Depends(get_agent_service),
) -> dict:
    """Verify an agent's API key and return {agent_id, name}.

    Side effect: writes ``request.state.agent_id`` so the rate limiter can
    bucket per-agent (see ``_rate_limit_key``).

    Results are cached in-memory for 60 s (max 10 000 entries) to reduce Redis lookups.
    """
    if not authorization.startswith("Bearer "):
        _record_auth_failure(reason="bearer_format_invalid", request=request)
        raise HTTPException(
            status_code=401,
            detail="Invalid authorization header format, expected: Bearer <API_KEY>",
        )
    api_key = authorization[7:]
    agent_info = await _resolve_agent_by_bearer(api_key, agent_service, request=request)
    request.state.agent_id = agent_info["agent_id"]
    request.state.rate_limit_key = f"agent:{agent_info['agent_id']}"
    return agent_info


AgentApiKeyDep = Annotated[dict, Depends(verify_agent_api_key)]


async def verify_proxy_caller(
    request: Request,
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
        raise HTTPException(
            status_code=401,
            detail="Invalid X-ACN-Authorization format, expected: Bearer <API_KEY>",
        )
    api_key = x_acn_authorization[7:]
    agent_info = await _resolve_agent_by_bearer(api_key, agent_service, request=request)
    request.state.agent_id = agent_info["agent_id"]
    request.state.rate_limit_key = f"agent:{agent_info['agent_id']}"
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
        raise HTTPException(status_code=403, detail="Invalid internal token")


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
        raise HTTPException(status_code=403, detail="Invalid internal token")

    # Fall back to owner-via-API-key.
    if not authorization or not authorization.startswith("Bearer "):
        _record_auth_failure(reason="owner_or_internal_missing_credential", request=request)
        raise HTTPException(
            status_code=401,
            detail=(
                "Owner API key (Authorization: Bearer <API_KEY>) "
                "or X-Internal-Token required"
            ),
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
        raise HTTPException(
            status_code=403,
            detail="API key does not match agent_id",
        )
    request.state.agent_id = agent_info["agent_id"]
    request.state.rate_limit_key = f"agent:{agent_info['agent_id']}"
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
        raise HTTPException(
            status_code=422,
            detail=(
                "from_agent must match 'system:<slug>' "
                "where <slug> is 1-64 chars of [A-Za-z0-9_-] "
                "(internal channel reserves the 'system:' namespace)"
            ),
        )
