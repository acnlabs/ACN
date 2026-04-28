"""Agent Registry API Routes

Clean Architecture implementation: Route → Service → Repository

Supports two registration modes:
1. Platform Registration (managed): POST /register - requires Auth0
2. Autonomous Join: POST /join - no auth, returns API key
3. Self-service: GET /me - agent gets own info via API key
"""

import re
import secrets
from typing import Literal

import httpx
import structlog  # type: ignore[import-untyped]
from a2a.types import AgentCapabilities, AgentCard, AgentSkill  # type: ignore[import-untyped]
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from ..auth.middleware import require_permission, verify_token
from ..config import Settings, get_settings
from ..core.exceptions import AgentNotFoundException
from ..models import AgentInfo, AgentRegisterRequest, AgentRegisterResponse, AgentSearchResponse
from ..monitoring import AuditEventType, AuditLevel, fire_and_forget_event, get_audit_singleton
from ..security import SSRFViolation, safe_resolve_target, validate_endpoint_url
from ..services.rewards_client import RewardsClient
from .dependencies import (  # type: ignore[import-untyped]
    AgentApiKeyDep,
    AgentIdPath,
    AgentServiceDep,
    InternalTokenDep,
    ProxyCallerDep,
    SubnetManagerDep,
    # Underscore-prefixed crossing of module boundaries is intentional:
    # ``_get_real_ip`` is the canonical proxy-aware IP resolver and we
    # need the SSRF audit hook to attribute attacks to the real client,
    # not to the front proxy. Lifting it to a public name would split
    # ownership; keep the import explicit + commented instead.
    _get_real_ip,
    limiter,
)

router = APIRouter(prefix="/api/v1/agents", tags=["registry"])
logger = structlog.get_logger()
settings = get_settings()


# ========== Request/Response Models ==========


class AgentJoinRequest(BaseModel):
    """Request for autonomous agent to join ACN"""

    name: str = Field(..., min_length=2, max_length=100, description="Agent name")
    description: str = Field(..., min_length=10, max_length=500, description="What this agent does (required)")
    tags: list[str] = Field(default_factory=list, max_length=20, description="Capability tags (e.g. ['coding', 'search']). Optional but recommended for discoverability.")
    endpoint: str = Field(..., max_length=500, description="Agent A2A endpoint URL (must be http/https)")
    referrer_id: str | None = Field(None, max_length=128, description="Referrer agent ID")
    # `agent_card` is a structured A2A Agent Card; total payload size is
    # bounded by BodySizeLimitMiddleware (security audit H6) — we don't
    # impose a Pydantic-level dict cap here because the A2A schema itself
    # already constrains shape.
    agent_card: dict | None = Field(None, description="A2A Agent Card (protocol v0.3.0)")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name cannot be blank")
        # Reject auto-generated names: ends with 8+ digit numeric suffix (e.g. agent-1772498556)
        if re.search(r"[-_]\d{8,}$", v):
            raise ValueError(
                "Name looks auto-generated (ends with a long numeric suffix). "
                "Please use a descriptive human-readable name."
            )
        # Must contain at least one letter (Latin or CJK)
        if not re.search(r"[a-zA-Z\u4e00-\u9fff]", v):
            raise ValueError("Name must contain at least one letter.")
        return v

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^https?://", v, re.IGNORECASE):
            raise ValueError("Endpoint must be an http:// or https:// URL.")
        # SSRF guard: reject IP-literal endpoints in private/reserved ranges.
        # Hostname resolution is checked again at dispatch time (see
        # `_proxy_to_agent`) to defend against DNS rebinding.
        try:
            validate_endpoint_url(v, allow_loopback=settings.dev_mode)
        except SSRFViolation as e:
            raise ValueError(str(e)) from e
        return v
    # Payment capability (optional — can be set later via POST /payments/{id}/payment-capability)
    wallet_addresses: dict[str, str] = Field(
        default_factory=dict,
        description="Per-network wallet addresses, e.g. {'ethereum': '0x...', 'base': '0x...'}",
    )
    wallet_address: str | None = Field(
        default=None,
        max_length=128,
        description="Legacy single wallet address (auto-mapped to wallet_addresses['ethereum'])",
    )
    accepts_payment: bool = Field(default=False, description="Whether agent accepts payments")
    payment_methods: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Accepted payment methods, e.g. ['usdc', 'eth', 'platform_credits']",
    )
    token_pricing: dict | None = Field(
        default=None,
        description="Token-based pricing, e.g. {'input_price_per_million': 3.0, 'output_price_per_million': 15.0, 'currency': 'USD'}",
    )


class AgentJoinResponse(BaseModel):
    """Response after agent joins ACN"""

    agent_id: str = Field(..., description="Assigned agent ID")
    api_key: str = Field(..., description="API key for authentication - SAVE THIS!")
    status: str = Field(default="active", description="Agent status")
    claim_status: str = Field(default="unclaimed", description="Claim status")
    verification_code: str = Field(..., description="Code for human verification")

    # Helpful endpoints
    claim_url: str = Field(..., description="URL for human to claim this agent")
    referral_url: str = Field(..., description="Share this URL so other agents register under your referral")
    tasks_endpoint: str = Field(..., description="Endpoint to fetch tasks")
    heartbeat_endpoint: str = Field(..., description="Heartbeat endpoint")
    agent_card_url: str = Field(..., description="URL to retrieve the stored Agent Card")


class AgentClaimRequest(BaseModel):
    """Request to claim an agent"""

    verification_code: str = Field(..., max_length=128, description="One-time claim token (returned at registration)")


class AgentClaimResponse(BaseModel):
    """Response after claiming an agent"""

    success: bool
    agent_id: str
    owner: str | None
    message: str


class AgentTransferRequest(BaseModel):
    """Request to transfer agent ownership"""

    new_owner: str = Field(..., max_length=128, description="New owner identifier")


class AgentTransferResponse(BaseModel):
    """Response after transferring agent"""

    success: bool
    agent_id: str
    previous_owner: str
    new_owner: str
    message: str


class AgentReleaseResponse(BaseModel):
    """Response after releasing agent ownership"""

    success: bool
    agent_id: str
    previous_owner: str
    message: str


class AgentMeResponse(BaseModel):
    """Response for /me endpoint - agent's own information"""

    agent_id: str
    name: str
    description: str | None = None
    tags: list[str] = []
    status: str
    claim_status: str
    owner: str | None = None
    # [REMOVED] balance, total_earned, owner_share - 由 Backend Wallet API 管理
    registered_at: str | None = None
    last_heartbeat: str | None = None
    # Helpful endpoints
    tasks_endpoint: str
    heartbeat_endpoint: str


# ============================================================================
# 🔧 DEV MODE: Register without Auth (for local development only)
# ============================================================================
@router.post("/dev/register", response_model=AgentRegisterResponse)
async def dev_register_agent(
    request: AgentRegisterRequest,
    agent_service: AgentServiceDep = None,
    subnet_manager: SubnetManagerDep = None,
):
    """DEV MODE: Register an Agent without Auth0 (local development only)

    ⚠️ WARNING: This endpoint should be disabled in production!
    """
    if not settings.dev_mode:
        raise HTTPException(
            status_code=403,
            detail="Dev mode registration is disabled. Use /register with Auth0 token.",
        )

    logger.warning(
        "DEV MODE: Registering agent without authentication", owner=request.owner, name=request.name
    )

    # Get subnet IDs
    subnet_ids = request.get_subnet_ids()

    # Validate subnets
    for subnet_id in subnet_ids:
        if subnet_id != "public" and not subnet_manager.subnet_exists(subnet_id):
            raise HTTPException(
                status_code=400,
                detail=f"Subnet not found: {subnet_id}",
            )

    try:
        # Use AgentService (Clean Architecture)
        agent = await agent_service.register_agent(
            owner=request.owner,
            name=request.name,
            endpoint=request.endpoint,
            tags=request.tags,
            subnet_ids=subnet_ids,
            agent_card=request.agent_card,
        )

        # Return response
        return AgentRegisterResponse(
            agent_id=agent.agent_id,
            name=agent.name,
            status=agent.status.value,
            registered_at=agent.registered_at,
            message=f"DEV MODE: Agent registered successfully (owner: {request.owner})",
        )

    except Exception as e:
        logger.error("Dev registration failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


def _agent_entity_to_info(agent, *, strip_sensitive: bool = False) -> AgentInfo:
    """Convert Agent entity to AgentInfo model.

    When strip_sensitive=True (e.g. public list/detail):
    - verification_code is omitted from metadata
    - raw endpoint is replaced with the ACN-unified communication address
      so callers are always routed through ACN instead of contacting agents directly.
    """
    metadata = {
        **agent.metadata,
        "claim_status": agent.claim_status.value if agent.claim_status else None,
        "referrer_id": agent.referrer_id,
    }
    if not strip_sensitive:
        metadata["verification_code"] = agent.verification_code

    # Public-facing endpoint: ACN canonical address (hides the real backend URL).
    # Owner-only access (strip_sensitive=False) keeps the raw endpoint for debugging.
    if strip_sensitive:
        base_url = settings.gateway_base_url or f"http://localhost:{settings.port}"
        exposed_endpoint = f"{base_url}/api/v1/agents/{agent.agent_id}"
    else:
        exposed_endpoint = agent.endpoint or ""

    return AgentInfo(
        agent_id=agent.agent_id,
        owner=agent.owner or "unowned",
        name=agent.name,
        description=agent.description,
        endpoint=exposed_endpoint,
        tags=agent.tags,
        status=agent.status.value,
        subnet_ids=agent.subnet_ids,
        agent_card=agent.agent_card,
        metadata=metadata,
        registered_at=agent.registered_at,
        last_heartbeat=agent.last_heartbeat,
        wallet_address=agent.wallet_address,
        wallet_addresses=agent.wallet_addresses or None,
        accepts_payment=agent.accepts_payment,
        payment_methods=agent.payment_methods,
    )


@router.post("/register", response_model=AgentRegisterResponse)
async def register_agent(
    request: AgentRegisterRequest,
    payload: dict = Depends(require_permission("acn:write")),
    agent_service: AgentServiceDep = None,
    subnet_manager: SubnetManagerDep = None,
):
    """Register an Agent (Idempotent) - Requires Auth0 Token

    Clean Architecture: Route → AgentService → Repository
    """
    token_owner: str = payload.get("sub", "")

    # Validate owner
    if request.owner != token_owner:
        permissions = payload.get("permissions", []) or payload.get("scope", "").split()
        if "acn:admin" not in permissions:
            raise HTTPException(
                status_code=403,
                detail=f"Cannot register agent for owner '{request.owner}'. Token owner is '{token_owner}'.",
            )

    # Get subnet IDs
    subnet_ids = request.get_subnet_ids()

    # Validate subnets
    for subnet_id in subnet_ids:
        if subnet_id != "public" and not subnet_manager.subnet_exists(subnet_id):
            raise HTTPException(
                status_code=400,
                detail=f"Subnet not found: {subnet_id}",
            )

    try:
        # Use AgentService (Clean Architecture)
        agent = await agent_service.register_agent(
            owner=request.owner,
            name=request.name,
            endpoint=request.endpoint,
            tags=request.tags,
            subnet_ids=subnet_ids,
            description=getattr(request, "description", None),
            metadata=getattr(request, "metadata", {}),
            agent_card=request.agent_card,
        )

        # Generate Agent Card URL
        base_url = settings.gateway_base_url or f"http://localhost:{settings.port}"
        agent_card_url = f"{base_url}/api/v1/agents/{agent.agent_id}/.well-known/agent-card.json"

        logger.info("agent_registered", agent_id=agent.agent_id, owner=agent.owner)

        return AgentRegisterResponse(
            status="registered",
            agent_id=agent.agent_id,
            name=agent.name,
            agent_card_url=agent_card_url,
        )
    except Exception as e:
        logger.error("agent_registration_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/me", response_model=AgentMeResponse)
async def get_my_agent(
    authorization: str = Header(..., description="Bearer API_KEY"),
    agent_service: AgentServiceDep = None,
):
    """
    Get current agent's own information via API key

    This endpoint allows agents to retrieve their own information
    without knowing their agent_id. Useful for self-service operations.

    Example:
        GET /api/v1/agents/me
        Authorization: Bearer acn_xxxxx
    """
    # Parse API key from Authorization header
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header format")

    api_key = authorization[7:]  # Remove "Bearer " prefix

    # Find agent by API key
    agent = await agent_service.get_agent_by_api_key(api_key)
    if not agent:
        raise HTTPException(status_code=401, detail="Invalid API key")

    base_url = settings.gateway_base_url or f"http://localhost:{settings.port}"

    return AgentMeResponse(
        agent_id=agent.agent_id,
        name=agent.name,
        description=agent.description,
        tags=agent.tags or [],
        status=agent.status.value,
        claim_status=agent.claim_status.value if agent.claim_status else "unclaimed",
        owner=agent.owner,
        registered_at=agent.registered_at.isoformat() if agent.registered_at else None,
        last_heartbeat=agent.last_heartbeat.isoformat() if agent.last_heartbeat else None,
        tasks_endpoint=f"{base_url}/api/v1/tasks",
        heartbeat_endpoint=f"{base_url}/api/v1/agents/{agent.agent_id}/heartbeat",
    )


@router.get("/unclaimed", response_model=AgentSearchResponse)
async def list_unclaimed_agents(
    _: InternalTokenDep,
    limit: int = 100,
    agent_service: AgentServiceDep = None,
):
    """
    List all unclaimed agents (requires X-Internal-Token)

    Returns agents that have joined but not been claimed by any owner.
    Restricted to ACN operators to prevent enumeration attacks.
    """
    agents = await agent_service.get_unclaimed_agents(limit=limit)
    agent_infos = [_agent_entity_to_info(a) for a in agents]

    return AgentSearchResponse(
        agents=agent_infos,
        total=len(agent_infos),
    )


@router.get("/{agent_id}", response_model=AgentInfo)
@limiter.limit("120/minute")
async def get_agent(request: Request, agent_id: AgentIdPath, agent_service: AgentServiceDep = None):
    """Get agent information (public discovery; verification_code not included)."""
    try:
        agent = await agent_service.get_agent(agent_id)
        return _agent_entity_to_info(agent, strip_sensitive=True)
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e


_PROXY_HOP_BY_HOP_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        # Strip ACN-internal auth so the downstream agent never sees the
        # caller's ACN API key. ``Authorization`` is intentionally kept —
        # callers may want to authenticate independently to the target
        # agent and that header is conceptually theirs.
        "x-acn-authorization",
        "x-internal-token",
    }
)


async def _proxy_to_agent(
    request: Request,
    agent_id: AgentIdPath,
    method: str,
    rest_path: str,
    agent_service,
    caller: dict,
) -> Response:
    """Generic reverse proxy: forward any HTTP method + optional sub-path to the agent's real endpoint.

    root POST  /{agent_id}          → {real_endpoint}               (A2A JSON-RPC)
    sub-path   /{agent_id}/foo/bar  → {real_endpoint}/foo/bar       (REST pass-through)

    ``caller`` is the verified ACN-side calling agent (``{agent_id, name}``).
    Its ID is forwarded as ``X-ACN-Caller-Agent`` so the target endpoint
    can attribute the request even though all proxied traffic appears to
    come from ACN's egress IP.
    """
    try:
        agent = await agent_service.get_agent(agent_id)
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e

    real_endpoint = agent.endpoint
    if not real_endpoint:
        raise HTTPException(status_code=503, detail="Agent has no registered endpoint")

    target_url = real_endpoint.rstrip("/")
    if rest_path:
        target_url = f"{target_url}/{rest_path.lstrip('/')}"

    # SSRF guard: re-resolve the target host *now* and reject if any DNS
    # answer points to a private/reserved range. This catches the
    # "register a public hostname, repoint DNS to 127.0.0.1 later" attack
    # that pure registration-time validation cannot stop.
    try:
        await safe_resolve_target(target_url, allow_loopback=settings.dev_mode)
    except SSRFViolation as e:
        logger.warning(
            "proxy_ssrf_blocked",
            agent_id=agent_id,
            target=target_url,
            reason=str(e),
        )
        # Audit trail — fire-and-forget so a misbehaving Redis can never
        # turn an SSRF block into a 500 (and amplify the attack surface).
        # Use ``_get_real_ip`` so the recorded ``source_ip`` honours
        # ``trusted_proxies`` (matches the auth-failure path; without this
        # we'd attribute every SSRF attempt to the front proxy).
        try:
            ssrf_src_ip: str | None = _get_real_ip(request)
        except Exception:  # noqa: BLE001 — never break the proxy path on diagnostics
            ssrf_src_ip = request.client.host if request.client else None
        fire_and_forget_event(
            get_audit_singleton(),
            event_type=AuditEventType.SECURITY_SSRF_BLOCKED,
            actor_id=caller.get("agent_id"),
            actor_type="agent",
            target_id=agent_id,
            target_type="agent",
            level=AuditLevel.WARNING,
            details={
                "target_url": target_url,
                "reason": str(e),
                "method": method,
            },
            source_ip=ssrf_src_ip,
        )
        raise HTTPException(status_code=502, detail="Agent endpoint is not reachable.") from e

    body = await request.body()
    forward_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _PROXY_HOP_BY_HOP_HEADERS
    }
    forward_headers["X-ACN-Caller-Agent"] = caller["agent_id"]
    if caller.get("name"):
        forward_headers["X-ACN-Caller-Name"] = caller["name"]

    try:
        # ``follow_redirects=False`` so a 3xx response cannot escape the SSRF
        # guard we just performed by pointing httpx at an internal address.
        client = httpx.AsyncClient(timeout=60.0, follow_redirects=False)
        req = client.build_request(method, target_url, content=body, headers=forward_headers)
        resp = await client.send(req, stream=True)

        content_type = resp.headers.get("content-type", "application/json")

        if "text/event-stream" in content_type:
            async def _stream():
                try:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
                finally:
                    await resp.aclose()
                    await client.aclose()

            return StreamingResponse(_stream(), status_code=resp.status_code, media_type=content_type)

        content = await resp.aread()
        await resp.aclose()
        await client.aclose()
        return Response(content=content, status_code=resp.status_code, media_type=content_type)

    except httpx.RequestError as e:
        logger.error(
            "a2a_proxy_error",
            agent_id=agent_id, method=method,
            target=target_url, error=str(e),
        )
        # ``from None`` (not ``from e``) — the httpx error chain may carry
        # connection-level details (resolved IPs, internal hostnames) that
        # we don't want surfacing in the client's exception trace. The
        # original error is already in the structured server log above.
        raise HTTPException(
            status_code=502, detail="Failed to reach agent endpoint"
        ) from None


async def _join_agent_impl(
    body: AgentJoinRequest,
    background_tasks: BackgroundTasks,
    ref: str | None,
    agent_service,
) -> AgentJoinResponse:
    """Shared implementation for join_agent and join_agent_internal."""
    try:
        referrer_id = body.referrer_id or ref

        wallet_addresses = dict(body.wallet_addresses)
        if body.wallet_address and "ethereum" not in wallet_addresses:
            wallet_addresses["ethereum"] = body.wallet_address

        agent, api_key = await agent_service.join_agent(
            name=body.name,
            description=body.description,
            tags=body.tags,
            endpoint=body.endpoint,
            referrer_id=referrer_id,
            agent_card=body.agent_card,
            wallet_addresses=wallet_addresses,
            accepts_payment=body.accepts_payment,
            payment_methods=body.payment_methods,
            token_pricing=body.token_pricing,
        )

        base_url = settings.gateway_base_url or f"http://localhost:{settings.port}"
        frontend_url = (settings.frontend_base_url or base_url).rstrip("/")

        logger.info("agent_joined", agent_id=agent.agent_id, name=agent.name, referrer_id=referrer_id)

        background_tasks.add_task(_grant_register_reward, agent_id=agent.agent_id)

        if referrer_id:
            background_tasks.add_task(_grant_referral_reward, referrer_id=referrer_id, new_agent_id=agent.agent_id)
            background_tasks.add_task(_increment_referral_count, referrer_id=referrer_id, agent_service=agent_service)

        claim_token = agent.verification_code or ""
        return AgentJoinResponse(
            agent_id=agent.agent_id,
            api_key=api_key,
            status=agent.status.value,
            claim_status=agent.claim_status.value if agent.claim_status else "unclaimed",
            verification_code=claim_token,
            claim_url=f"{frontend_url}/claim/{agent.agent_id}?token={claim_token}",
            referral_url=f"{base_url}/api/v1/agents/join?ref={agent.agent_id}",
            tasks_endpoint=f"{base_url}/api/v1/tasks",
            heartbeat_endpoint=f"{base_url}/api/v1/agents/{agent.agent_id}/heartbeat",
            agent_card_url=f"{base_url}/api/v1/agents/{agent.agent_id}/.well-known/agent-card.json",
        )
    except Exception as e:
        logger.error("agent_join_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/join/internal", response_model=AgentJoinResponse, include_in_schema=False)
async def join_agent_internal(
    request: Request,
    body: AgentJoinRequest,
    background_tasks: BackgroundTasks,
    ref: str | None = Query(None),
):
    """Internal join endpoint — no rate limit, requires X-Internal-Token."""
    token = request.headers.get("X-Internal-Token", "")
    if (
        not token
        or not settings.internal_api_token
        or not secrets.compare_digest(token, settings.internal_api_token)
    ):
        raise HTTPException(status_code=401, detail="Internal token required")
    # Get agent service manually to avoid FastAPI Depends() injection edge cases
    from .dependencies import get_agent_service as _get_svc
    try:
        agent_svc = _get_svc()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=f"Service unavailable: {e}") from e
    return await _join_agent_impl(body, background_tasks, ref, agent_svc)


@router.post("/join", response_model=AgentJoinResponse)
@limiter.limit("5/minute;50/day")
async def join_agent(
    request: Request,
    body: AgentJoinRequest,
    background_tasks: BackgroundTasks,
    ref: str | None = Query(None, description="Referrer agent ID (query param shortcut, body referrer_id takes priority)"),
    agent_service: AgentServiceDep = None,
):
    """
    Autonomous agent joins ACN (self-registration)

    No authentication required. Returns an API key for future requests.
    The agent will be in "unclaimed" status until a human claims it.

    Rate limits: 5/minute, 50/day per IP.

    Referral: pass ?ref={agent_id} in the URL (generated by the referrer's referral_url),
    or set referrer_id in the request body. Body takes priority over query param.

    Example:
        POST /api/v1/agents/join?ref=<referrer_agent_id>
        {
            "name": "MyAgent",
            "description": "An autonomous coding agent",
            "tags": ["coding", "review"],
            "endpoint": "https://my-agent.example.com/a2a"
        }
    """
    return await _join_agent_impl(body, background_tasks, ref, agent_service)


@router.post("/{agent_id}")
@limiter.limit("60/minute")
async def proxy_post(
    request: Request,
    agent_id: AgentIdPath,
    caller: ProxyCallerDep,
    agent_service: AgentServiceDep = None,
):
    """Proxy POST to agent's real endpoint — A2A JSON-RPC (message/send, message/stream, tasks/*).

    Requires ``X-ACN-Authorization: Bearer <ACN_API_KEY>`` to identify the calling
    agent; rate-limit is bucketed per-agent.
    """
    return await _proxy_to_agent(request, agent_id, "POST", "", agent_service, caller)


@router.put("/{agent_id}")
@limiter.limit("60/minute")
async def proxy_put(
    request: Request,
    agent_id: AgentIdPath,
    caller: ProxyCallerDep,
    agent_service: AgentServiceDep = None,
):
    """Proxy PUT to agent's real endpoint. Requires ``X-ACN-Authorization``."""
    return await _proxy_to_agent(request, agent_id, "PUT", "", agent_service, caller)


@router.patch("/{agent_id}")
@limiter.limit("60/minute")
async def proxy_patch(
    request: Request,
    agent_id: AgentIdPath,
    caller: ProxyCallerDep,
    agent_service: AgentServiceDep = None,
):
    """Proxy PATCH to agent's real endpoint. Requires ``X-ACN-Authorization``."""
    return await _proxy_to_agent(request, agent_id, "PATCH", "", agent_service, caller)


@router.get("", response_model=AgentSearchResponse)
@limiter.limit("60/minute")
async def search_agents(
    request: Request,
    tag: str | None = None,
    skill: str | None = None,  # Deprecated alias for `tag` — kept for backward compat
    status: Literal["online", "offline", "all"] = Query(
        default="online",
        description="Filter by status: online (recent heartbeat), offline, or all (all registered agents)",
    ),
    owner: str | None = None,
    name: str | None = None,
    agent_service: AgentServiceDep = None,
):
    """Search agents.

    Clean Architecture: Route → AgentService → Repository
    """
    tag_param = tag or skill  # accept both; `tag` takes precedence
    tag_list = tag_param.split(",") if tag_param else None

    # Search using AgentService
    agents = await agent_service.search_agents(
        tags=tag_list,
        status=status,
    )

    # Apply additional filters (owner, name)
    if owner:
        agents = [a for a in agents if a.owner == owner]
    if name:
        agents = [a for a in agents if name.lower() in a.name.lower()]

    # Convert to AgentInfo (public list: do not expose verification_code)
    agent_infos = [_agent_entity_to_info(a, strip_sensitive=True) for a in agents]

    return AgentSearchResponse(
        agents=agent_infos,
        total=len(agent_infos),
    )


@router.post("/{agent_id}/heartbeat")
async def agent_heartbeat(
    agent_id: AgentIdPath,
    agent_info: AgentApiKeyDep,
    agent_service: AgentServiceDep = None,
):
    """Update agent heartbeat (requires Agent API Key)

    The authenticated agent must match the path `agent_id` to prevent
    falsely keeping other agents alive.
    Clean Architecture: Route → AgentService → Repository
    """
    if agent_info["agent_id"] != agent_id:
        raise HTTPException(status_code=403, detail="API key does not match agent_id")
    try:
        await agent_service.update_heartbeat(agent_id)
        return {"status": "ok", "agent_id": agent_id}
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e


@router.get("/{agent_id}/.well-known/agent-card.json")
async def get_agent_card(agent_id: AgentIdPath, agent_service: AgentServiceDep = None):
    """Get agent's A2A Agent Card (v0.3.0 compliant)

    Returns the card submitted at registration time if available.
    Falls back to auto-generating a minimal card from stored fields.
    """
    try:
        agent = await agent_service.get_agent(agent_id)

        # Return the complete card submitted at registration (e.g. OpenPersona-generated)
        if agent.agent_card:
            return agent.agent_card

        # Fallback: auto-generate a minimal card from stored fields
        card = AgentCard(
            name=agent.name,
            version="0.1.0",
            description=agent.description or f"{agent.name} on ACN",
            url=agent.endpoint or "",
            capabilities=AgentCapabilities(streaming=False),
            default_input_modes=["text", "application/json"],
            default_output_modes=["text", "application/json"],
            tags=[
                AgentSkill(
                    id=skill,
                    name=skill.replace("-", " ").replace("_", " ").title(),
                    description=f"Capability: {skill}",
                    tags=[skill],
                )
                for skill in agent.tags
            ],
        )

        return card.model_dump(exclude_none=True)
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e


@router.get("/{agent_id}/.well-known/agent-registration.json")
async def get_agent_registration_file(
    agent_id: AgentIdPath,
    agent_service: AgentServiceDep = None,
    cfg: Settings = Depends(get_settings),
):
    """Get agent's ERC-8004 Registration File.

    This endpoint serves as the on-chain agentURI. It is separate from the
    A2A agent-card.json endpoint and follows the ERC-8004 registration file
    schema (type, name, description, services, registrations, x402Support).
    """
    from ..services.agent_service import build_erc8004_registration_file

    try:
        agent = await agent_service.get_agent(agent_id)
        return build_erc8004_registration_file(agent, cfg)
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e


@router.get("/{agent_id}/endpoint")
async def get_agent_endpoint(agent_id: AgentIdPath, agent_service: AgentServiceDep = None):
    """Get agent endpoint

    Clean Architecture: Route → AgentService → Repository
    """
    try:
        agent = await agent_service.get_agent(agent_id)
        return {"agent_id": agent_id, "endpoint": agent.endpoint}
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e


@router.delete("")
async def admin_bulk_delete_agents(
    request: Request,
    _: InternalTokenDep,
    agent_service: AgentServiceDep = None,
    name_prefix: str | None = None,
    owner: str | None = None,
    dry_run: bool = True,
):
    """Admin: bulk delete agents by name prefix or owner (requires X-Internal-Token).

    Use dry_run=true (default) to preview which agents would be deleted.
    Set dry_run=false to actually delete.

    Audit (security audit H-audit):
      - Each successful delete writes an ``AGENT_UNREGISTERED`` audit event
        attributed to ``actor_id="admin@internal"`` so destructive admin
        actions are individually traceable.
      - One ``ADMIN_BULK_DELETE`` summary event is written at the end with
        the filters and counts. This survives even when individual deletes
        fail, giving operators a single point to query "did anyone run a
        bulk delete with prefix X today?".
      - ``dry_run=True`` writes no audit events — preview is read-only.
      - All audit writes are awaited (not fire-and-forget) because admin
        delete actions are compliance-critical: losing an event is worse
        than the request taking a few extra ms.

    Safety guard (security audit H-audit follow-up):
      ``dry_run=False`` requires at least one of ``name_prefix`` / ``owner``.
      Without a filter the loop would target every registered agent — the
      INTERNAL_API_TOKEN gate is the only thing standing between an operator
      typo and a full-table wipe. The guard is intentionally NOT applied to
      ``dry_run=True`` so operators can preview the full population before
      choosing a filter.
    """
    if not dry_run and not name_prefix and not owner:
        raise HTTPException(
            status_code=400,
            detail=(
                "Refusing to bulk-delete without a filter. "
                "Pass name_prefix or owner explicitly. "
                "Use dry_run=true to preview filterless results."
            ),
        )

    agents = await agent_service.search_agents(tags=None, status="all")

    # Apply filters
    targets = agents
    if name_prefix:
        targets = [a for a in targets if a.name.startswith(name_prefix)]
    if owner is not None:
        targets = [a for a in targets if (a.owner or "unowned") == owner]

    if dry_run:
        return {
            "dry_run": True,
            "would_delete": len(targets),
            "agents": [{"agent_id": a.agent_id, "name": a.name, "owner": a.owner} for a in targets],
        }

    audit = get_audit_singleton()
    source_ip = request.client.host if request.client else None
    actor_id = request.headers.get("x-creator-id") or "admin@internal"

    deleted, failed = [], []
    for a in targets:
        try:
            await agent_service.repository.delete(a.agent_id)
            deleted.append(a.agent_id)
            logger.info("admin_bulk_delete", agent_id=a.agent_id, name=a.name)
            if audit is not None:
                try:
                    await audit.log_event(
                        event_type=AuditEventType.AGENT_UNREGISTERED,
                        actor_id=actor_id,
                        actor_type="system",
                        target_id=a.agent_id,
                        target_type="agent",
                        level=AuditLevel.WARNING,
                        details={
                            "via": "admin_bulk_delete",
                            "name": a.name,
                            "owner": a.owner,
                        },
                        source_ip=source_ip,
                    )
                except Exception as audit_exc:  # noqa: BLE001
                    logger.warning(
                        "admin_bulk_delete_audit_failed",
                        agent_id=a.agent_id,
                        error=str(audit_exc),
                    )
        except Exception as exc:
            failed.append({"agent_id": a.agent_id, "error": str(exc)})

    if audit is not None:
        try:
            await audit.log_event(
                event_type=AuditEventType.ADMIN_BULK_DELETE,
                actor_id=actor_id,
                actor_type="system",
                level=AuditLevel.WARNING,
                details={
                    "name_prefix": name_prefix,
                    "owner": owner,
                    "matched": len(targets),
                    "deleted": len(deleted),
                    "failed": len(failed),
                },
                source_ip=source_ip,
            )
        except Exception as audit_exc:  # noqa: BLE001
            logger.warning("admin_bulk_delete_summary_audit_failed", error=str(audit_exc))

    return {"dry_run": False, "deleted": len(deleted), "failed": len(failed), "failed_details": failed}


@router.delete("/{agent_id}")
async def unregister_agent(
    agent_id: AgentIdPath,
    payload: dict = Depends(require_permission("acn:write")),
    agent_service: AgentServiceDep = None,
):
    """Unregister an agent

    Clean Architecture: Route → AgentService → Repository
    """
    token_owner: str = payload.get("sub", "")

    try:
        # AgentService handles authorization check
        success = await agent_service.unregister_agent(agent_id, token_owner)

        if success:
            logger.info("agent_unregistered", agent_id=agent_id)
            return {"status": "unregistered", "agent_id": agent_id}
        else:
            raise HTTPException(status_code=404, detail="Agent not found")
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e


# ============================================================================
# Autonomous Agent Endpoints (No Auth0 required)
# ============================================================================


async def _grant_register_reward(agent_id: str) -> None:
    """Background task: grant register_agent reward to a newly joined agent."""
    try:
        rewards_client = RewardsClient(
            backend_url=settings.backend_url,
            internal_token=settings.internal_api_token,
        )
        result = await rewards_client.grant_register_bonus(agent_id=agent_id)
        if result.success:
            logger.info("register_reward_granted", agent_id=agent_id, amount=result.amount)
        else:
            logger.warning("register_reward_failed", agent_id=agent_id, error=result.error)
    except Exception as e:
        logger.error("register_reward_error", agent_id=agent_id, error=str(e))


async def _grant_claim_reward(agent_id: str, user_id: str) -> None:
    """Background task: grant claim_agent reward to the user who claimed."""
    try:
        rewards_client = RewardsClient(
            backend_url=settings.backend_url,
            internal_token=settings.internal_api_token,
        )
        result = await rewards_client.grant_claim_bonus(agent_id=agent_id, user_id=user_id)
        if result.success:
            logger.info("claim_reward_granted", agent_id=agent_id, user_id=user_id, amount=result.amount)
        else:
            logger.warning("claim_reward_failed", agent_id=agent_id, user_id=user_id, error=result.error)
    except Exception as e:
        logger.error("claim_reward_error", agent_id=agent_id, user_id=user_id, error=str(e))


async def _grant_referral_reward(referrer_id: str, new_agent_id: str) -> None:
    """Background task to grant referral reward"""
    try:
        rewards_client = RewardsClient(
            backend_url=settings.backend_url,
            internal_token=settings.internal_api_token,
        )
        result = await rewards_client.grant_referral_bonus(
            referrer_id=referrer_id,
            new_agent_id=new_agent_id,
        )
        if result.success:
            logger.info(
                "referral_reward_granted",
                referrer_id=referrer_id,
                new_agent_id=new_agent_id,
                amount=result.amount,
            )
        else:
            logger.warning(
                "referral_reward_failed",
                referrer_id=referrer_id,
                new_agent_id=new_agent_id,
                error=result.error,
            )
    except Exception as e:
        logger.error(
            "referral_reward_error",
            referrer_id=referrer_id,
            new_agent_id=new_agent_id,
            error=str(e),
        )


async def _increment_referral_count(referrer_id: str, agent_service) -> None:
    """Background task: increment referrer's referral_count in metadata"""
    try:
        referrer = await agent_service.get_agent(referrer_id)
        if referrer:
            metadata = dict(referrer.metadata or {})
            metadata["referral_count"] = int(metadata.get("referral_count", 0)) + 1
            referrer.metadata = metadata
            await agent_service.repository.save(referrer)
            logger.info(
                "referral_count_incremented",
                referrer_id=referrer_id,
                new_count=metadata["referral_count"],
            )
    except Exception as e:
        logger.error("referral_count_error", referrer_id=referrer_id, error=str(e))


@router.post("/{agent_id}/claim", response_model=AgentClaimResponse)
async def claim_agent(
    agent_id: AgentIdPath,
    request: AgentClaimRequest,
    background_tasks: BackgroundTasks,
    payload: dict = Depends(verify_token),
    agent_service: AgentServiceDep = None,
):
    """
    Claim ownership of an unclaimed agent

    Requires Auth0 authentication. The authenticated user becomes the owner.
    """
    token_owner: str = payload.get("sub", "")

    try:
        agent = await agent_service.claim_agent(
            agent_id=agent_id,
            owner=token_owner,
            verification_code=request.verification_code,
        )

        logger.info("agent_claimed", agent_id=agent_id, owner=token_owner)

        # Grant claim_agent reward to the user who claimed
        background_tasks.add_task(
            _grant_claim_reward,
            agent_id=agent_id,
            user_id=token_owner,
        )

        return AgentClaimResponse(
            success=True,
            agent_id=agent.agent_id,
            owner=agent.owner,
            message=f"Agent '{agent.name}' successfully claimed",
        )
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/{agent_id}/transfer", response_model=AgentTransferResponse)
async def transfer_agent(
    agent_id: AgentIdPath,
    request: AgentTransferRequest,
    payload: dict = Depends(require_permission("acn:write")),
    agent_service: AgentServiceDep = None,
):
    """
    Transfer agent ownership to another user

    Only the current owner can transfer the agent.
    """
    token_owner: str = payload.get("sub", "")

    try:
        agent = await agent_service.transfer_agent(
            agent_id=agent_id,
            current_owner=token_owner,
            new_owner=request.new_owner,
        )

        logger.info(
            "agent_transferred",
            agent_id=agent_id,
            from_owner=token_owner,
            to_owner=request.new_owner,
        )

        return AgentTransferResponse(
            success=True,
            agent_id=agent.agent_id,
            previous_owner=token_owner,
            new_owner=agent.owner,
            message=f"Agent '{agent.name}' transferred to {request.new_owner}",
        )
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e


@router.post("/{agent_id}/release", response_model=AgentReleaseResponse)
async def release_agent(
    agent_id: AgentIdPath,
    payload: dict = Depends(require_permission("acn:write")),
    agent_service: AgentServiceDep = None,
):
    """
    Release ownership of an agent (make it unowned/unclaimed)

    Only the current owner can release the agent.
    After release, anyone can claim the agent again.
    """
    token_owner: str = payload.get("sub", "")

    try:
        agent = await agent_service.release_agent(
            agent_id=agent_id,
            owner=token_owner,
        )

        logger.info("agent_released", agent_id=agent_id, previous_owner=token_owner)

        return AgentReleaseResponse(
            success=True,
            agent_id=agent.agent_id,
            previous_owner=token_owner,
            message=f"Agent '{agent.name}' released. It can now be claimed by anyone.",
        )
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e


# ============================================================================
# Agent Wallet Management API
# ============================================================================


# [REMOVED] Agent Wallet endpoints - 前端直接调 Backend API:
#   GET  /api/agent-wallets/{agent_id}           获取钱包
#   POST /api/agent-wallets/{agent_id}/topup     充值
#   POST /api/agent-wallets/{agent_id}/withdraw  提取


# [DELETED] set_agent_owner_share endpoint - 不再支持 owner_share 分成机制


class AgentWalletsResponse(BaseModel):
    """Unified wallet view for an agent — aggregates all payment account info."""

    agent_id: str
    accepts_payment: bool
    payment_methods: list[str]
    wallet_addresses: dict[str, str] = Field(
        description="Per-network wallet addresses, key = network name (ethereum/base/solana/...)"
    )
    platform_credits_id: str = Field(
        description="Agent's platform credits account ID (same as agent_id)"
    )
    token_pricing: dict | None = Field(
        default=None,
        description="Token-based pricing config (input/output price per million tokens)",
    )
    pricing: dict = Field(
        default_factory=dict,
        description="Fixed pricing per skill (e.g. {'coding': '50.00'})",
    )
    payment_processor: str | None = Field(
        default=None,
        description="Traditional payment processor (e.g. 'stripe', 'paypal')",
    )
    erc8004: dict | None = Field(
        default=None,
        description="On-chain ERC-8004 identity info if registered",
    )


@router.get("/{agent_id}/wallets", response_model=AgentWalletsResponse)
async def get_agent_wallets(
    agent_id: AgentIdPath,
    agent_service: AgentServiceDep = None,
):
    """
    Get unified wallet and payment capability view for an agent.

    Aggregates all payment account information: multi-chain crypto addresses,
    platform credits, pricing, and on-chain identity. Use this as the single
    source of truth for agent payment info (e.g. for AgentBooks economy faculty).
    """
    try:
        agent = await agent_service.get_agent(agent_id)
    except AgentNotFoundException:
        raise HTTPException(status_code=404, detail="Agent not found") from None

    erc8004 = None
    if agent.erc8004_agent_id:
        erc8004 = {
            "token_id": agent.erc8004_agent_id,
            "chain": agent.erc8004_chain,
            "tx_hash": agent.erc8004_tx_hash,
            "registered_at": agent.erc8004_registered_at.isoformat()
            if agent.erc8004_registered_at
            else None,
        }

    return AgentWalletsResponse(
        agent_id=agent.agent_id,
        accepts_payment=agent.accepts_payment,
        payment_methods=agent.payment_methods,
        wallet_addresses=agent.wallet_addresses,
        platform_credits_id=agent.agent_id,
        token_pricing=agent.token_pricing,
        pricing={},
        payment_processor=None,
        erc8004=erc8004,
    )


# ── Catch-all proxy ───────────────────────────────────────────────────────────
# Must be registered LAST so all ACN-native sub-routes (heartbeat, claim,
# transfer, wallets, .well-known/*, etc.) take precedence.
# Proxies any unmatched sub-path + HTTP method to the agent's real endpoint.

@router.api_route("/{agent_id}/{rest_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@limiter.limit("60/minute")
async def proxy_subpath(
    request: Request,
    agent_id: AgentIdPath,
    rest_path: str,
    caller: ProxyCallerDep,
    agent_service: AgentServiceDep = None,
):
    """Catch-all reverse proxy for agent sub-paths.

    Any request to /{agent_id}/{rest_path} that is not handled by an
    ACN-native route is transparently forwarded to the agent's real
    endpoint at {real_endpoint}/{rest_path}, preserving method, headers,
    and body.  SSE streaming is supported for text/event-stream responses.

    Requires ``X-ACN-Authorization: Bearer <ACN_API_KEY>``; rate-limit
    bucketed per calling agent.
    """
    return await _proxy_to_agent(
        request, agent_id, request.method, rest_path, agent_service, caller
    )
