"""FastAPI Dependencies for ACN

Provides dependency injection for core services.
"""

import re
import secrets
import time
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
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
from ..services import AgentService, BillingService, MessageService, SubnetService
from ..services.activity_service import ActivityService

settings = get_settings()


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
    global _activity_service

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
