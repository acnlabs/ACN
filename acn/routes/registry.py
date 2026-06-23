"""Agent Registry API Routes

Clean Architecture implementation: Route → Service → Repository

Supports two registration modes:
1. Platform Registration (managed): POST /register - requires Auth0
2. Autonomous Join: POST /join - no auth, returns API key
3. Self-service: GET /me - agent gets own info via API key
"""

import asyncio
import base64
import json
import re
import secrets
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote, urlparse

import httpx
import structlog  # type: ignore[import-untyped]
from a2a.compat.v0_3.types import (  # type: ignore[import-untyped]
    AgentCapabilities,
    AgentCard,
    AgentSkill,
)
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    Security,
)
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator, model_validator

from ..auth.middleware import require_permission, verify_token
from ..config import Settings, get_settings
from ..core.errors import ACN_DEFAULT_RESPONSES, ACNHTTPError, ErrorCode
from ..core.exceptions import AgentNotFoundException, PolicyRejected
from ..core.validators import check_dict_size_64k
from ..models import AgentInfo, AgentRegisterRequest, AgentRegisterResponse, AgentSearchResponse
from ..monitoring import AuditEventType, AuditLevel, fire_and_forget_event, get_audit_singleton
from ..security import SSRFViolation, safe_resolve_target, validate_endpoint_url
from ..services.rewards_client import RewardsClient
from .dependencies import (  # type: ignore[import-untyped]
    WALLET_RATE_LIMIT,
    AgentApiKeyDep,
    AgentIdPath,
    AgentServiceDep,
    InternalTokenDep,
    ManifestServiceDep,
    MetricsDep,
    OwnerOrInternalDep,
    PolicyServiceDep,
    ProxyCallerDep,
    SubnetManagerDep,
    SubnetServiceDep,
    # Underscore-prefixed crossing of module boundaries is intentional:
    # ``_get_real_ip`` is the canonical proxy-aware IP resolver and we
    # need the SSRF audit hook to attribute attacks to the real client,
    # not to the front proxy. Lifting it to a public name would split
    # ownership; keep the import explicit + commented instead.
    _get_real_ip,
    # ``_wallet_rate_limit_key`` is the L418 secondary key_func used by
    # ``@limiter.limit(WALLET_RATE_LIMIT, key_func=...)`` on the four
    # proxy entry points below. Same module-private rationale as
    # ``_get_real_ip``: lives next to the limiter definition, and
    # exposing it as a public name would force a shim layer that adds
    # zero value.
    _wallet_rate_limit_key,
    evict_agent_from_cache,
    limiter,
    verify_owner_or_internal,
)

router = APIRouter(
    prefix="/api/v1/agents",
    tags=["registry"],
    responses=ACN_DEFAULT_RESPONSES,
)
logger = structlog.get_logger()
settings = get_settings()
_optional_bearer = HTTPBearer(auto_error=False)


# Phase 2 review v2 P1 #10 — modes that carry an implicit SDK-version
# contract. When a PATCH /policy resolves to one of these, the agent
# becomes deaf to the legacy ``agent_message`` WS event and instead
# only receives ``manifest_notification`` (manifest mode) or
# inbox-suppressed traffic that the SDK must consult by polling
# ``/communication/manifest/{id}`` (allowlist non-member targets).
# An old SDK that doesn't implement those handlers silently misses
# every inbound message, which is a near-impossible-to-diagnose
# breakage from the agent author's perspective.
#
# We surface ``X-ACN-SDK-Min-Version`` on every PATCH that *resolves*
# to one of these modes — including idempotent re-applications —
# rather than only on a transition (open→manifest). Idempotent calls
# are the most common form of "deploy script confirms desired state",
# so always emitting the header makes the SDK contract observable
# from any single PATCH the operator inspects, not only the first
# one. See L601 of
# ``docs/features/acn-communication-economic-model.md``.
_MODES_REQUIRING_SDK_NOTIFY: frozenset[str] = frozenset({"manifest", "allowlist"})
_SDK_MIN_VERSION_HEADER = "X-ACN-SDK-Min-Version"

# Modes that push messages to the agent over HTTP and therefore require a
# delivery endpoint. ``manifest`` / ``closed`` never push (pull / reject)
# so they may register and operate without one.
_PUSH_MODES: frozenset[str] = frozenset({"open", "allowlist"})


def _validate_agent_endpoint_url(v: str | None) -> str | None:
    """Validate an agent-supplied delivery URL (shared by join + PATCH).

    Returns the stripped URL, or ``None`` for a ``None`` / blank input.
    Raises ``ValueError`` on any of:
      * non-http(s) scheme,
      * a host that points at ACN's own gateway (which would pass the
        reachability probe via ACN's 405 yet deliver nothing to the agent),
      * an SSRF-blocked target (private / reserved IP literal).

    Centralizing this keeps the registration field validator and the
    ``PATCH /{id}/endpoint`` route in lockstep — a divergence here would
    let an endpoint banned at join time slip in via the update path.
    """
    if v is None:
        return None
    v = v.strip()
    if not v:
        return None
    if not re.match(r"^https?://", v, re.IGNORECASE):
        raise ValueError("Endpoint must be an http:// or https:// URL.")
    _v_host = urlparse(v).netloc.lower()
    for _gateway in (settings.gateway_base_url, settings.frontend_base_url):
        if _gateway:
            _gw_host = urlparse(_gateway.rstrip("/")).netloc.lower()
            if _v_host and _gw_host and _v_host == _gw_host:
                raise ValueError(
                    "Endpoint must point to the agent's own server, "
                    "not to the ACN gateway."
                )
    try:
        validate_endpoint_url(v, allow_loopback=settings.dev_mode)
    except SSRFViolation as e:
        raise ValueError(str(e)) from e
    return v


# ========== Request/Response Models ==========


def _validate_agent_name(v: str) -> str:
    """Validate an agent display name (shared by join + profile PATCH).

    Returns the stripped name. Raises ``ValueError`` on a blank name, an
    auto-generated-looking name (long trailing numeric suffix), or a name
    with no letters. Centralizing this keeps the registration validator
    and the ``PATCH /{id}/profile`` route enforcing the same rule — a
    divergence would let a name banned at join time slip in via edit.
    """
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


class AgentJoinRequest(BaseModel):
    """Request for autonomous agent to join ACN"""

    name: str = Field(..., min_length=2, max_length=100, description="Agent name")
    description: str = Field(..., min_length=10, max_length=500, description="What this agent does (required)")
    tags: list[str] = Field(default_factory=list, max_length=20, description="Capability tags (e.g. ['coding', 'search']). Optional but recommended for discoverability.")
    endpoint: str | None = Field(
        None,
        max_length=500,
        description="[Deprecated] Direct A2A JSON-RPC endpoint URL. Use a2a_endpoint.",
    )
    a2a_endpoint: str | None = Field(
        None,
        max_length=500,
        description=(
            "Direct A2A JSON-RPC endpoint URL used for message delivery. MUST "
            "be the COMPLETE URL your A2A server listens on, including any path "
            "(e.g. https://host/a2a, not https://host) — ACN posts each message "
            "to this exact URL verbatim and never appends a path. A handshake "
            "probe at registration returns a2a_handshake_ok=false if the URL "
            "responds but is not a JSON-RPC endpoint (null if the probe is "
            "indeterminate, e.g. it timed out)."
        ),
    )
    agent_card_url: str | None = Field(
        None,
        max_length=500,
        description=(
            "A2A Agent Card discovery URL. If a2a_endpoint is omitted, ACN "
            "fetches this card and extracts the JSON-RPC endpoint."
        ),
    )
    delivery: str | None = Field(
        None,
        description=(
            "Inbound delivery transport (ADR-0012). 'direct' (default): ACN "
            "dials the agent's public endpoint / agent_card_url. 'relay': the "
            "agent holds an outbound WebSocket (`acn listen`) and ACN pushes "
            "messages over it in real time — no public delivery URL required."
        ),
    )
    referrer_id: str | None = Field(None, max_length=128, description="Referrer agent ID")
    agent_card: dict | None = Field(None, description="A2A Agent Card (protocol v0.3.0)")
    self_hosted: bool = Field(
        default=False,
        description=(
            "True if the agent is operated by its human owner directly (the "
            "owner holds the API key). When such an agent changes owner "
            "(transfer-invite claim or transfer), ACN rotates the API key so "
            "the previous owner is locked out, and returns the fresh key to "
            "the new owner. Leave false for platform-managed agents whose key "
            "is held by a hosting operator (e.g. AgentMother) — those keep the "
            "key and the operator re-keys on the owner_changed event instead."
        ),
    )

    @field_validator("agent_card")
    @classmethod
    def _agent_card_size(cls, v: dict | None) -> dict | None:
        if v is None:
            return v
        return check_dict_size_64k("agent_card", v)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return _validate_agent_name(v)

    @field_validator("endpoint", "a2a_endpoint", "agent_card_url")
    @classmethod
    def validate_endpoint(cls, v: str | None) -> str | None:
        # SSRF / scheme / ACN-gateway-host checks are shared with the
        # PATCH /{id}/endpoint route via ``_validate_agent_endpoint_url``.
        # Hostname resolution is re-checked at dispatch time (see
        # ``_proxy_to_agent``) to defend against DNS rebinding.
        return _validate_agent_endpoint_url(v)

    @field_validator("delivery")
    @classmethod
    def validate_delivery(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if v not in {"direct", "relay"}:
            raise ValueError("delivery must be 'direct' or 'relay'")
        return v

    @model_validator(mode="after")
    def require_delivery_or_discovery_url(self):
        # Endpoint requirement depends on the inbound delivery model:
        # - ``open`` / ``allowlist`` push to the agent over HTTP, so they
        #   need a delivery endpoint.
        # - ``manifest`` (the default) and ``closed`` never push — manifest
        #   agents pull from ``GET /communication/manifest/{id}``; closed
        #   agents reject all inbound. Both can register without any URL.
        # This keeps the default registration path open to pull-only AI
        # assistants and local-dev agents that have no public HTTP server.
        policy_mode = (self.communication_policy or {}).get("mode", "manifest")
        # ADR-0012 Mode B: relay-delivery agents are pushed to over their
        # outbound WebSocket (`acn listen`), so a push-mode agent may join
        # without any public delivery URL when it opts into relay. A direct
        # URL is mutually exclusive with relay in every mode — accepting both
        # would silently dial over HTTP (Mode A) and ignore the relay intent.
        if self.delivery == "relay":
            if self.a2a_endpoint or self.endpoint or self.agent_card_url:
                raise ValueError(
                    "delivery='relay' is mutually exclusive with a delivery URL "
                    "(a2a_endpoint, endpoint, or agent_card_url): relay agents are "
                    "reached only over their outbound WebSocket. Omit the URL, or "
                    "use delivery='direct' to be dialled over HTTP."
                )
            return self
        if policy_mode in {"manifest", "closed"}:
            return self
        if not (self.a2a_endpoint or self.endpoint or self.agent_card_url):
            raise ValueError(
                f"communication_policy.mode={policy_mode!r} requires a delivery URL. "
                "Pass a2a_endpoint, endpoint, or agent_card_url, OR omit "
                "communication_policy to use the default 'manifest' (pull-based) "
                "mode which does not need a public endpoint."
            )
        return self

    def get_direct_a2a_endpoint(self) -> str | None:
        """Return the explicit direct delivery URL, if provided."""
        return self.a2a_endpoint or self.endpoint
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
    # Phase 3: new agents default to ``manifest`` mode so senders
    # must go through the Notify layer before the full message reaches
    # the recipient. Existing agents keep their stored policy
    # (``Agent.__post_init__`` backfills ``None`` → ``open`` for
    # rows that predate this change). Callers can always override
    # by passing an explicit ``communication_policy``.
    communication_policy: dict | None = Field(
        default={"mode": "manifest"},
        description=(
            "Inbound message policy. Accepts "
            "{'mode': 'open' | 'closed' | 'manifest' | 'allowlist', "
            "'reject_reason'?: str}. "
            "Default: manifest (Phase 3+)."
        ),
    )
    # Optional SOCIAL.md pointer — see https://agentsocial.one. ACN stores
    # only the URL; the body lives at the URL and consumers fetch on demand.
    social_card_url: str | None = Field(
        default=None,
        max_length=2048,
        description=(
            "URL to this agent's SOCIAL.md (https://agentsocial.one spec). "
            "Body is fetched on demand by consumers — ACN never caches it."
        ),
    )

    @field_validator("communication_policy")
    @classmethod
    def validate_communication_policy(cls, v):
        # Centralized validator in PolicyCheckService keeps the join
        # path, register path, and PATCH /policy endpoint in lockstep.
        from ..services.policy_service import validate_policy_dict

        return validate_policy_dict(v)

    @field_validator("social_card_url")
    @classmethod
    def validate_social_card_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if not (v.lower().startswith("https://") or v.lower().startswith("http://")):
            raise ValueError("social_card_url must start with https:// or http://")
        return v


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

    # Reachability probe result (soft check — False means the server didn't respond
    # to a HEAD probe at registration time, but registration still succeeded).
    endpoint_reachable: bool = Field(
        default=True,
        description=(
            "Whether a delivery endpoint is registered AND responded to a "
            "health probe at registration time. False when (a) no endpoint "
            "was registered (pull-only manifest/closed agents), or (b) the "
            "endpoint was registered but did not answer the probe. Callers "
            "should treat False as 'do not attempt direct delivery' and "
            "consult communication_mode to pick the right send path."
        ),
    )

    # A2A handshake probe result (soft, tri-state). Distinct from
    # endpoint_reachable: a host can be reachable (any HTTP response) yet NOT
    # speak A2A at the exact registered URL — the bare-origin / wrong-path
    # footgun where ACN's direct push silently 404s. Never a failure.
    a2a_handshake_ok: bool | None = Field(
        default=None,
        description=(
            "Tri-state result of the JSON-RPC handshake probe against the "
            "registered endpoint. true = confirmed A2A endpoint. false (with "
            "endpoint_reachable true) = the host responded but this exact URL "
            "is NOT an A2A JSON-RPC endpoint — verify the path (e.g. it should "
            "be https://host/a2a, not https://host). null = indeterminate "
            "(probe timed out / no endpoint probed) — no conclusion, could be "
            "a slow-but-valid server. Soft signal: registration always "
            "succeeds regardless."
        ),
    )

    # Resolved inbound delivery mode, echoed back so the caller does not
    # have to read it from the agent record. One of
    # ``open | manifest | allowlist | closed``.
    communication_mode: str = Field(
        default="manifest",
        description=(
            "Resolved inbound delivery mode: open | manifest | allowlist | "
            "closed. Echoed from the agent's communication_policy."
        ),
    )

    # Actionable next-step guidance for the operator. Populated when the
    # registration outcome is not the simple ``open + reachable endpoint``
    # happy path so the caller knows what to do next (poll the manifest
    # queue, deploy an endpoint, etc.). ``None`` means no follow-up is
    # required.
    next_step_hint: str | None = Field(
        default=None,
        description=(
            "Human-readable hint describing what the operator should do "
            "next — present for pull-only and unreachable-endpoint outcomes."
        ),
    )


def _extract_jsonrpc_endpoint_from_agent_card(agent_card: dict) -> str | None:
    """Return the direct JSON-RPC URL from v1 interfaces or legacy v0.3 url."""
    interfaces = (
        agent_card.get("supportedInterfaces")
        or agent_card.get("supported_interfaces")
        or []
    )
    for interface in interfaces:
        protocol_binding = (
            interface.get("protocolBinding")
            or interface.get("protocol_binding")
            or ""
        )
        if protocol_binding.upper() == "JSONRPC" and interface.get("url"):
            return interface["url"]

    # Legacy v0.3 cards expose the direct delivery URL as `url`.
    url = agent_card.get("url")
    return url if isinstance(url, str) and url else None


def _validate_resolved_a2a_endpoint(endpoint: str) -> None:
    """Apply registration-time endpoint validation to URLs parsed from cards."""
    try:
        validate_endpoint_url(endpoint, allow_loopback=settings.dev_mode)
    except SSRFViolation as e:
        logger.warning("endpoint_validation_rejected", endpoint=endpoint, reason=str(e))
        raise HTTPException(
            status_code=400, detail="The provided endpoint URL is not allowed."
        ) from e


async def _probe_endpoint_http(endpoint: str, *, timeout: float = 3.0) -> bool:
    """Send a HEAD request to *endpoint*; return True if any HTTP response arrives.

    A JSON-RPC-only server that rejects HEAD with 405 Method Not Allowed is
    still considered reachable — any HTTP response proves the host is live.
    Returns False on connection errors, timeouts, or SSL handshake failures.
    The caller decides whether to hard-block or surface a soft warning.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            await client.head(endpoint)
        return True
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError):
        return False
    except Exception:  # noqa: BLE001 — any other transport failure = unreachable
        return False


# A2A handshake probe. The HEAD reachability probe above only proves "some HTTP
# server answers at this host" — an nginx 404 counts as reachable. That gap let
# agents register a bare origin (e.g. https://host) while their A2A server is
# actually mounted at /a2a, so ACN's direct push (which POSTs to the VERBATIM
# registered URL) silently 404'd every delivered message and parked it in the
# inbox. The handshake probe closes that gap: it POSTs a JSON-RPC request with a
# deliberately-unknown method. A compliant A2A / JSON-RPC server answers with a
# structured error (``-32601 method not found``) WITHOUT executing anything,
# while a bare origin / wrong path / non-A2A server returns HTML, a redirect, or
# a connection error. See scripts/audit_push_mode_paths.sql for the audit that
# surfaced this failure class (agentmother / Samantha).
_A2A_HANDSHAKE_METHOD = "__acn_handshake_probe__"


async def _probe_a2a_handshake(endpoint: str, *, timeout: float = 8.0) -> bool | None:
    """Probe whether *endpoint* answers a JSON-RPC request like an A2A server.

    Tri-state, because "the host did not answer in time" and "the host answered
    but is not A2A" are very different signals and must not be conflated:

    - ``True``  — got a JSON-RPC-shaped response → confirmed A2A endpoint.
    - ``False`` — got a response that is DEFINITELY not JSON-RPC (HTML body,
      non-JSON content-type, JSON that is not an RPC envelope). High-confidence
      "wrong path / not A2A" (the nginx-404 / bare-origin case).
    - ``None``  — indeterminate: timeout, connection error, or any transport
      failure. Could be a slow-but-valid A2A server (agents that do real work
      on every request can take >timeout to answer even an unknown method), so
      callers MUST NOT surface a "not A2A" warning on ``None``.

    SOFT signal only — never blocks registration regardless of the result.
    The timeout is generous (vs the HEAD probe's 3s) precisely so a slow valid
    server resolves to None rather than a false ``False``.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": "acn-handshake-probe",
        "method": _A2A_HANDSHAKE_METHOD,
        "params": {},
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.post(
                endpoint,
                json=payload,
                headers={"content-type": "application/json"},
            )
    except Exception:  # noqa: BLE001 — timeout / transport failure → indeterminate
        return None
    # We got a response, so we CAN make a determination. A JSON-RPC server
    # commonly answers method-not-found with HTTP 200 + an error object, so we
    # key off the BODY shape, not the status code. A bare origin / wrong path
    # returns HTML (nginx 404) which fails the JSON-RPC checks below → False.
    if "json" not in resp.headers.get("content-type", "").lower():
        return False
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 — non-JSON body = not JSON-RPC
        return False
    if not isinstance(data, dict):
        return False
    if "jsonrpc" in data or "result" in data:
        return True
    err = data.get("error")
    return isinstance(err, dict) and isinstance(err.get("code"), int)


async def _check_endpoint_reachability(endpoint: str) -> bool:
    """Run the two-layer reachability check for a direct A2A endpoint.

    Layer 1 — DNS resolution (hard fail):
        Calls ``safe_resolve_target`` so that a hostname that can't be
        resolved (NXDOMAIN) or resolves to a private/blocked range is
        rejected immediately with a 400. This catches typos and
        not-yet-provisioned DNS records before they enter the registry.

    Layer 2 — HTTP HEAD probe (soft warning):
        Calls ``_probe_endpoint_http``; failures are logged but do NOT
        block registration. The caller receives the result as
        ``endpoint_reachable`` in the join/register response so that
        agents can detect misconfigured servers without being hard-rejected
        (the server may not be deployed yet at join time).

    Raises:
        HTTPException(400): if DNS resolution or SSRF guard fails.

    Returns:
        True if the HTTP probe succeeds, False if the probe times out or
        the connection is refused (i.e. DNS resolved but server is down).
    """
    try:
        await safe_resolve_target(endpoint, allow_loopback=settings.dev_mode)
    except SSRFViolation as e:
        logger.warning("endpoint_dns_check_failed", endpoint=endpoint, reason=str(e))
        raise HTTPException(
            status_code=400,
            detail="Endpoint host cannot be resolved or is not in an allowed address range.",
        ) from e

    reachable = await _probe_endpoint_http(endpoint)
    if not reachable:
        logger.warning("endpoint_http_probe_failed", endpoint=endpoint)
        raise HTTPException(
            status_code=400,
            detail=(
                "Endpoint did not respond to a reachability probe. "
                "Make sure the server is running and publicly accessible before registering. "
                "If you do not have a public endpoint, set communication_policy.mode to "
                "'closed' and omit the endpoint field (inbox-only operation)."
            ),
        )
    return reachable


def _build_next_step_hint(
    *,
    mode: str,
    has_endpoint: bool,
    endpoint_reachable: bool,
    a2a_handshake_ok: bool | None = True,
    agent_id: str,
    base_url: str,
) -> str | None:
    """Return an actionable follow-up message for non-happy-path registrations.

    Returns ``None`` for the simple ``open + reachable endpoint`` case
    where the caller has nothing to do. For every other shape, the hint
    spells out the concrete next API call the operator should make.
    """
    # Non-pushing modes never push over HTTP, so their hint is about how to
    # actually receive traffic — independent of whether an (unprobed)
    # endpoint was supplied. These modes are not reachability-probed, so
    # the "didn't answer the probe" branch below must not apply to them.
    if mode == "manifest":
        return (
            "Registered in pull-based 'manifest' mode. "
            f"Poll GET {base_url}/api/v1/communication/manifest/{agent_id} "
            "to receive notifications. To enable direct delivery later, "
            f"PATCH {base_url}/api/v1/agents/{agent_id}/endpoint with your "
            f"HTTPS URL and PATCH {base_url}/api/v1/agents/{agent_id}/policy "
            "with {\"mode\": \"open\"}."
        )
    if mode == "closed":
        return (
            "Registered in 'closed' mode — all inbound messages are "
            "rejected. To start receiving messages, PATCH "
            f"{base_url}/api/v1/agents/{agent_id}/policy with "
            "{\"mode\": \"manifest\"} (pull) or {\"mode\": \"open\"} (push)."
        )
    # Push modes (open / allowlist): the endpoint is load-bearing and was
    # probed. Surface a hint only when it didn't answer.
    if has_endpoint and not endpoint_reachable:
        return (
            "Endpoint registered but did not answer the reachability probe. "
            "Messages will fall back to the inbox queue until your server "
            "responds. Once the server is up, no further action is required."
        )
    # Reachable host that CONFIRMED it does not speak A2A JSON-RPC — the
    # bare-origin / wrong-path footgun. ACN posts the A2A message to the
    # VERBATIM registered URL, so a host that answers HTTP at / but mounts its
    # A2A server at /a2a will silently 404 every delivered message. Surface a
    # concrete fix (re-point the endpoint at the real A2A path). NOTE: only a
    # confirmed ``False`` triggers this — ``None`` (timeout / slow server) is
    # indeterminate and must not mislabel a slow-but-valid agent as "not A2A".
    if has_endpoint and endpoint_reachable and a2a_handshake_ok is False:
        return (
            "Endpoint is reachable but did NOT respond as an A2A JSON-RPC "
            "server. ACN delivers to this exact URL, so if your A2A server is "
            "mounted at a sub-path (e.g. /a2a) you must register that full "
            f"URL: PATCH {base_url}/api/v1/agents/{agent_id}/endpoint with the "
            "complete A2A JSON-RPC URL. Until then, direct delivery will fall "
            "back to the inbox queue."
        )
    return None


async def _resolve_registration_endpoint(
    *,
    direct_endpoint: str | None,
    agent_card_url: str | None,
    agent_card: dict | None,
) -> tuple[str, dict | None, bool, bool | None]:
    """Resolve the direct A2A endpoint while preserving the discovery card.

    Returns ``(endpoint, agent_card, endpoint_reachable, a2a_handshake_ok)``.

    ``endpoint_reachable`` reflects the HTTP HEAD probe result:
    - True  → server responded (any HTTP status counts as reachable)
    - False → DNS resolved but no HTTP response within the probe timeout

    ``a2a_handshake_ok`` is the tri-state JSON-RPC handshake probe result (see
    ``_probe_a2a_handshake``): ``True`` confirmed A2A, ``False`` confirmed NOT
    A2A (the bare-origin / wrong-path footgun), ``None`` indeterminate (timeout
    / slow server). It is strictly a soft signal — never blocks registration —
    and only a confirmed ``False`` should drive a "wrong path" warning so a
    slow-but-valid server is not mislabelled.

    DNS failures are hard errors (raise HTTPException); HTTP probe
    failures are soft — callers propagate the flag to the client so
    the agent operator knows the server isn't answering yet.
    """
    if direct_endpoint:
        _validate_resolved_a2a_endpoint(direct_endpoint)
        reachable = await _check_endpoint_reachability(direct_endpoint)
        handshake_ok = await _probe_a2a_handshake(direct_endpoint) if reachable else None
        return direct_endpoint, agent_card, reachable, handshake_ok

    if agent_card:
        direct_endpoint = _extract_jsonrpc_endpoint_from_agent_card(agent_card)
        if direct_endpoint:
            _validate_resolved_a2a_endpoint(direct_endpoint)
            reachable = await _check_endpoint_reachability(direct_endpoint)
            handshake_ok = (
                await _probe_a2a_handshake(direct_endpoint) if reachable else None
            )
            return direct_endpoint, agent_card, reachable, handshake_ok

    if not agent_card_url:
        raise HTTPException(
            status_code=422,
            detail="a2a_endpoint, endpoint, or agent_card_url is required",
        )

    try:
        await safe_resolve_target(agent_card_url, allow_loopback=settings.dev_mode)
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            response = await client.get(agent_card_url)
            response.raise_for_status()
            fetched_card = response.json()
    except (httpx.HTTPError, ValueError, SSRFViolation) as e:
        logger.warning("agent_card_fetch_failed", url=agent_card_url, reason=str(e))
        raise HTTPException(
            status_code=400,
            detail="Unable to fetch the A2A Agent Card from the provided URL.",
        ) from e

    direct_endpoint = _extract_jsonrpc_endpoint_from_agent_card(fetched_card)
    if not direct_endpoint:
        raise HTTPException(
            status_code=400,
            detail="A2A Agent Card does not include a JSON-RPC delivery URL",
        )
    _validate_resolved_a2a_endpoint(direct_endpoint)
    # The card URL was already fetched successfully, so at minimum the card host
    # is alive. Still probe the extracted endpoint — it may differ from the card URL.
    reachable = await _check_endpoint_reachability(direct_endpoint)
    handshake_ok = await _probe_a2a_handshake(direct_endpoint) if reachable else None

    return direct_endpoint, fetched_card, reachable, handshake_ok


class AgentClaimRequest(BaseModel):
    """Request to claim an agent"""

    verification_code: str = Field(..., max_length=128, description="One-time claim token (returned at registration)")


class AgentClaimResponse(BaseModel):
    """Response after claiming an agent"""

    success: bool
    agent_id: str
    owner: str | None
    message: str
    # Present only on a transfer-invite claim of an autonomous agent: a freshly
    # rotated plaintext API key, returned exactly once so the new owner can
    # (re)deploy their own instance. The giver's old key is now invalid.
    api_key: str | None = None


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


class AgentTransferInviteRequest(BaseModel):
    """Request to create a transfer invite"""

    ttl_seconds: int | None = Field(
        default=None,
        ge=60,
        description="Invite TTL in seconds (default 7 days, capped by server config)",
    )


class AgentTransferInviteResponse(BaseModel):
    """Response after creating a transfer invite"""

    agent_id: str
    verification_code: str = Field(..., description="One-time claim token for the recipient")
    expires_at: str = Field(..., description="ISO8601 expiry timestamp")


class AgentTransferInviteCancelResponse(BaseModel):
    """Response after cancelling a transfer invite"""

    success: bool
    agent_id: str
    message: str


class AgentRotateKeyResponse(BaseModel):
    """Response after rotating an agent's API key (H1).

    The new plaintext ``api_key`` is returned exactly once — the server
    stores only its SHA-256 hash. Callers MUST persist it before the
    response is discarded, otherwise the agent will need another
    rotation by its owner to recover.
    """

    success: bool
    agent_id: str
    api_key: str = Field(
        ...,
        description=(
            "New plaintext API key. Shown exactly once; server stores only its hash."
        ),
    )
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
        raise ACNHTTPError(
            ErrorCode.MISSING_PERMISSION,
            403,
            message="Dev mode registration is disabled in this environment. Use /register with an Auth0 token instead.",
            details={"reason": "dev_mode_disabled"},
        )

    logger.warning(
        "DEV MODE: Registering agent without authentication", owner=request.owner, name=request.name
    )

    # Get subnet IDs
    subnet_ids = request.get_subnet_ids()

    # Validate subnets
    for slug in subnet_ids:
        if slug != "public" and not subnet_manager.subnet_exists(slug):
            raise ACNHTTPError(
                ErrorCode.SUBNET_NOT_FOUND,
                400,
                details={"slug": slug},
            )

    try:
        # Manifest/closed agents are inbox-only (no push), so skip endpoint
        # resolution/probing — mirrors the join path (#142). Default (None)
        # is treated as ``open`` to preserve the legacy register contract.
        _policy_mode = (request.communication_policy or {}).get("mode", "open")
        # ADR-0012 Mode B: relay-delivery agents are reached over their
        # outbound WebSocket (`acn listen`), never dialled — skip endpoint
        # resolution and store no direct URL even in push modes.
        if _policy_mode in _PUSH_MODES and request.delivery != "relay":
            endpoint, agent_card, _, _ = await _resolve_registration_endpoint(
                direct_endpoint=request.get_direct_a2a_endpoint(),
                agent_card_url=request.agent_card_url,
                agent_card=request.agent_card,
            )
        else:
            endpoint = request.get_direct_a2a_endpoint()
            agent_card = request.agent_card
        # Use AgentService (Clean Architecture)
        agent = await agent_service.register_agent(
            owner=request.owner,
            name=request.name,
            endpoint=endpoint,
            a2a_endpoint=endpoint,
            tags=request.tags,
            subnet_ids=subnet_ids,
            agent_card=agent_card,
            communication_policy=request.communication_policy,
            agent_card_url=request.agent_card_url,
            social_card_url=request.social_card_url,
        )

        return AgentRegisterResponse(
            agent_id=agent.agent_id,
            name=agent.name,
            status="online" if await agent_service.is_alive(agent.agent_id) else "offline",
            registered_at=agent.registered_at,
            message=f"DEV MODE: Agent registered successfully (owner: {request.owner})",
        )

    except ACNHTTPError:
        # P3 cross-module catch-all defence: ``ACNHTTPError`` is
        # ``Exception``-typed (not ``HTTPException``-typed); without
        # this re-raise, any caller-actionable 4xx raised inside the
        # try body would be silently rewritten as a sanitised 500.
        raise
    except HTTPException:
        # Mirror defence for legacy ``HTTPException`` raises — same
        # swallow risk via the catch-all below.
        raise
    except Exception as e:
        logger.error("Dev registration failed", error=str(e))
        raise HTTPException(status_code=500, detail="Agent registration failed") from e


def _agent_entity_to_info(
    agent,
    *,
    is_online: bool,
    strip_sensitive: bool = False,
    public_subnet_slugs: set[str] | None = None,
) -> AgentInfo:
    """Convert Agent entity to AgentInfo model.

    The ``is_online`` argument is **required** and must come from the
    single source of truth (``AgentService.is_alive`` /
    ``batch_alive``) — the legacy ``Agent.status`` column is no longer
    consulted on read paths so callers MUST resolve alive-ness
    explicitly. Use :py:func:`_agent_entity_to_info_with_alive` /
    :py:func:`_agent_entities_to_infos` to do the lookup automatically.

    When ``strip_sensitive=True`` (e.g. public list/detail):
    - ``verification_code`` is omitted from metadata
    - raw endpoint is replaced with the ACN-unified communication address
      so callers are always routed through ACN instead of contacting agents
      directly.

    ``public_subnet_slugs`` controls subnet_ids visibility (ACL V6 B3):
    - ``None`` — full list (self API key or admin caller).
    - ``set[str]`` — filter to only slugs present in this set (everyone
      else). Prevents private subnet slugs from leaking to callers who
      have no membership relationship with those subnets.
    """
    metadata = {
        **agent.metadata,
        "claim_status": agent.claim_status.value if agent.claim_status else None,
        "referrer_id": agent.referrer_id,
    }
    if not strip_sensitive:
        metadata["verification_code"] = agent.verification_code

    # Pending-deletion marker. The raw ``deletion_request`` carries the
    # one-time token's SHA-256 hash, which must never appear in any read
    # response — replace it with a non-secret, outward-visible marker so
    # consumers can tell an agent is winding down (and stop routing fresh
    # work to it) without exposing the confirmation token.
    _deletion_request = metadata.pop("deletion_request", None)
    if isinstance(_deletion_request, dict):
        metadata["pending_deletion"] = {
            "requested_at": _deletion_request.get("requested_at"),
            "expires_at": _deletion_request.get("expires_at"),
        }

    # Public-facing endpoint: ACN canonical address (hides the real backend URL).
    # Owner-only access (strip_sensitive=False) keeps the raw endpoint for debugging.
    if strip_sensitive:
        base_url = settings.gateway_base_url or f"http://localhost:{settings.port}"
        exposed_endpoint = f"{base_url}/api/v1/agents/{agent.agent_id}"
        exposed_agent_card_url = f"{exposed_endpoint}/.well-known/agent-card.json"
        exposed_agent_card = None
    else:
        exposed_endpoint = agent.endpoint or ""
        exposed_agent_card_url = getattr(agent, "agent_card_url", None)
        exposed_agent_card = agent.agent_card

    # ACL V6 B3: filter subnet_ids to public slugs when the caller is not
    # the agent itself (self API key) and does not hold acn:admin.
    if public_subnet_slugs is None:
        exposed_subnet_ids = agent.subnet_ids
    else:
        exposed_subnet_ids = [s for s in agent.subnet_ids if s in public_subnet_slugs]

    return AgentInfo(
        agent_id=agent.agent_id,
        owner=agent.owner or "unowned",
        name=agent.name,
        description=agent.description,
        endpoint=exposed_endpoint,
        a2a_endpoint=exposed_endpoint,
        agent_card_url=exposed_agent_card_url,
        tags=agent.tags,
        status="online" if is_online else "offline",
        subnet_ids=exposed_subnet_ids,
        agent_card=exposed_agent_card,
        metadata=metadata,
        registered_at=agent.registered_at,
        last_heartbeat=agent.last_heartbeat,
        wallet_address=agent.wallet_address,
        wallet_addresses=agent.wallet_addresses or None,
        accepts_payment=agent.accepts_payment,
        payment_methods=agent.payment_methods,
        social_card_url=agent.social_card_url,
    )


async def _agent_entity_to_info_with_alive(
    agent,
    *,
    agent_service,
    strip_sensitive: bool = False,
    public_subnet_slugs: set[str] | None = None,
) -> AgentInfo:
    """Async wrapper that resolves ``is_online`` from Redis alive (single shot)."""
    is_online = await agent_service.is_alive(agent.agent_id)
    return _agent_entity_to_info(
        agent,
        is_online=is_online,
        strip_sensitive=strip_sensitive,
        public_subnet_slugs=public_subnet_slugs,
    )


async def _agent_entities_to_infos(
    agents: list,
    *,
    agent_service,
    strip_sensitive: bool = False,
    public_subnet_slugs: set[str] | None = None,
) -> list[AgentInfo]:
    """Async batch variant — one Redis round-trip for the whole list.

    Use this in every listing path. Looping over the single-shot helper
    would issue one Redis EXISTS per agent, which we already worked
    hard to avoid for ``filter_alive`` callers.
    """
    if not agents:
        return []
    alive_ids = await agent_service.batch_alive([a.agent_id for a in agents])
    return [
        _agent_entity_to_info(
            a,
            is_online=a.agent_id in alive_ids,
            strip_sensitive=strip_sensitive,
            public_subnet_slugs=public_subnet_slugs,
        )
        for a in agents
    ]


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
            raise ACNHTTPError(
                ErrorCode.OWNERSHIP_MISMATCH,
                403,
                message=(
                    f"Cannot register agent for owner '{request.owner}'. "
                    f"Token owner is '{token_owner}'."
                ),
                details={
                    "requested_owner": request.owner,
                    "token_owner": token_owner,
                },
            )

    # Get subnet IDs
    subnet_ids = request.get_subnet_ids()

    # Validate subnets
    for slug in subnet_ids:
        if slug != "public" and not subnet_manager.subnet_exists(slug):
            raise ACNHTTPError(
                ErrorCode.SUBNET_NOT_FOUND,
                400,
                details={"slug": slug},
            )

    try:
        # Manifest/closed agents are inbox-only (no push), so skip endpoint
        # resolution/probing — mirrors the join path (#142). Default (None)
        # is treated as ``open`` to preserve the legacy register contract.
        _policy_mode = (request.communication_policy or {}).get("mode", "open")
        # ADR-0012 Mode B: relay-delivery agents are reached over their
        # outbound WebSocket (`acn listen`), never dialled — skip endpoint
        # resolution and store no direct URL even in push modes.
        if _policy_mode in _PUSH_MODES and request.delivery != "relay":
            endpoint, agent_card, _, _ = await _resolve_registration_endpoint(
                direct_endpoint=request.get_direct_a2a_endpoint(),
                agent_card_url=request.agent_card_url,
                agent_card=request.agent_card,
            )
        else:
            endpoint = request.get_direct_a2a_endpoint()
            agent_card = request.agent_card
        # Use AgentService (Clean Architecture)
        agent = await agent_service.register_agent(
            owner=request.owner,
            name=request.name,
            endpoint=endpoint,
            a2a_endpoint=endpoint,
            tags=request.tags,
            subnet_ids=subnet_ids,
            description=getattr(request, "description", None),
            metadata=getattr(request, "metadata", {}),
            agent_card=agent_card,
            communication_policy=request.communication_policy,
            agent_card_url=request.agent_card_url,
            social_card_url=request.social_card_url,
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
    except ACNHTTPError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error("agent_registration_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Agent registration failed") from e


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
        raise ACNHTTPError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            401,
            message="Authorization header must use the 'Bearer <api_key>' format.",
            details={"reason": "invalid_authorization_header_format"},
        )

    api_key = authorization[7:]  # Remove "Bearer " prefix

    # Find agent by API key
    agent = await agent_service.get_agent_by_api_key(api_key)
    if not agent:
        raise ACNHTTPError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            401,
            message="The supplied API key is invalid or has been revoked.",
            details={"reason": "invalid_api_key"},
        )

    base_url = settings.gateway_base_url or f"http://localhost:{settings.port}"

    return AgentMeResponse(
        agent_id=agent.agent_id,
        name=agent.name,
        description=agent.description,
        tags=agent.tags or [],
        status="online" if await agent_service.is_alive(agent.agent_id) else "offline",
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
    agent_infos = await _agent_entities_to_infos(
        agents, agent_service=agent_service
    )

    return AgentSearchResponse(
        agents=agent_infos,
        total=len(agent_infos),
    )


async def _get_public_subnet_slugs(subnet_service) -> set[str]:
    """Return the set of public subnet slugs from the repository.

    Called once per request on the agent-read paths; the result is used
    to filter ``subnet_ids`` for non-self / non-admin callers (ACL V6 B3).
    Degrades to the minimal ``{"public"}`` set on any lookup failure so
    the read path never 500s due to a subnet-service error.
    """
    try:
        public_subnets = await subnet_service.list_public_subnets()
        return {s.slug for s in public_subnets}
    except Exception:  # noqa: BLE001
        return {"public"}


def _caller_gets_full_subnet_ids(payload: dict, agent_id: str) -> bool:
    """Return True when the caller is entitled to the full subnet_ids list.

    Full access is granted to:
    - The agent itself (API key caller whose ``sub == agent_id``).
    - ``acn:admin`` (platform ops).

    Everyone else — including the agent's human owner — receives only
    public subnet slugs (ACL V6 B3).  The human owner can inspect the
    full list via ``GET /agents/me`` using the agent's own API key.
    """
    if "acn:admin" in payload.get("permissions", []):
        return True
    return payload.get("type") == "agent" and payload.get("sub") == agent_id


@router.get("/{agent_id}", response_model=AgentInfo)
@limiter.limit("120/minute")
async def get_agent(
    request: Request,
    agent_id: AgentIdPath,
    credentials: HTTPAuthorizationCredentials | None = Security(_optional_bearer),
    agent_service: AgentServiceDep = None,
    subnet_service: SubnetServiceDep = None,
):
    """Get agent information (public discovery; verification_code not included).

    Populates ``followers_count`` / ``follows_count`` from the follow
    graph (proposal §数据模型). Falls back to ``0`` if the follow
    subsystem is not wired or its lookup fails — a missing count must
    never block agent retrieval.

    ``subnet_ids`` visibility (ACL V6 B3): the agent itself (self API
    key) and ``acn:admin`` receive the full list. All other callers —
    including the agent's human owner — see only public subnet slugs.
    This prevents private subnet names from leaking to anyone who
    queries a public agent endpoint.
    """
    # Resolve optional caller payload for subnet_ids ACL.
    caller_payload: dict | None = None
    if credentials:
        try:
            caller_payload = await verify_token(request, credentials)
        except Exception:  # noqa: BLE001 — invalid token → treat as anon
            caller_payload = None

    full_ids = caller_payload is not None and _caller_gets_full_subnet_ids(
        caller_payload, agent_id
    )
    public_slugs: set[str] | None = None if full_ids else await _get_public_subnet_slugs(
        subnet_service
    )

    try:
        agent = await agent_service.get_agent(agent_id)
        info = await _agent_entity_to_info_with_alive(
            agent,
            agent_service=agent_service,
            strip_sensitive=True,
            public_subnet_slugs=public_slugs,
        )
        # Inbound reachability (single-agent read only — avoids the extra
        # per-agent Redis round-trip on listing paths). Best-effort: a lookup
        # failure must never block agent retrieval, so the fields stay None.
        try:
            health = await agent_service.get_inbound_health(agent_id)
            # Only trust a real mapping — a non-dict (e.g. a test double's
            # auto-mock) would otherwise leak unserializable values such as a
            # ``.get`` coroutine into the response model and 500 the read.
            if isinstance(health, dict):
                info.inbound_reachable = health.get("reachable")
                info.last_inbound_ok_at = health.get("last_ok_at")
                info.consec_push_failures = health.get("consec_fail")
        except Exception as e:  # noqa: BLE001 — diagnostic field is best-effort
            logger.warning("inbound_health_lookup_failed", agent_id=agent_id, error=str(e))
        try:
            from . import dependencies as _deps

            follow_svc = _deps._follow_service
            if follow_svc is not None:
                following, followers = await follow_svc.get_counts(agent_id)
                info.follows_count = following
                info.followers_count = followers
        except Exception as e:  # noqa: BLE001 — counts are best-effort
            logger.warning("follow_counts_lookup_failed", agent_id=agent_id, error=str(e))
        return info
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e


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
    policy_service=None,
    metrics=None,
) -> Response:
    """Generic reverse proxy: forward any HTTP method + optional sub-path to the agent's real endpoint.

    root POST  /{agent_id}          → {real_endpoint}               (A2A JSON-RPC)
    sub-path   /{agent_id}/foo/bar  → {real_endpoint}/foo/bar       (REST pass-through)

    ``caller`` is the verified ACN-side calling agent (``{agent_id, name}``).
    Its ID is forwarded as ``X-ACN-Caller-Agent`` so the target endpoint
    can attribute the request even though all proxied traffic appears to
    come from ACN's egress IP.

    ``policy_service`` (Phase 1) gates the proxy at the gateway boundary:
    a recipient with ``communication_policy.mode == "closed"`` short-
    circuits with HTTP 403 *before* any DNS/SSRF/HTTP work fires, so a
    leaked ACN API key cannot push traffic at agents that opted out.
    Without this hook the four reverse-proxy endpoints would be a
    structural bypass of the policy gate that ``MessageRouter`` and
    ``SubnetManager`` enforce on every other path.
    """
    try:
        agent = await agent_service.get_agent(agent_id)
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e

    # Gateway-level access control. Mirrors MessageRouter.route() — same
    # service, same exemption rules, same error shape — so a client that
    # already handles 403/communication_rejected on /communication/send
    # gets identical wire behaviour through the proxy. Done before
    # endpoint discovery / SSRF resolution / HTTP client init: the
    # rejection must produce zero observable side effects toward the
    # recipient's network.
    if policy_service is not None:
        try:
            # Phase 2 PR #2: ``check_inbound_or_raise`` is now ``async``.
            # The proxy intentionally does NOT thread an
            # ``is_in_allowlist`` callback here — proxy-mode is a
            # closed/open gate only, and ``allowlist`` mode would be
            # surfaced as "divert to manifest" which is meaningless for
            # raw HTTP proxy semantics. With no callback,
            # ``check_inbound`` falls back to "divert to manifest"
            # internally, which the proxy then treats as "allow"
            # (decision.allow=True, no raise) — matching the
            # legacy proxy behaviour for non-closed modes.
            await policy_service.check_inbound_or_raise(
                sender_id=caller.get("agent_id", "unknown"),
                recipient_id=agent_id,
                recipient_policy=getattr(agent, "communication_policy", None),
            )
        except PolicyRejected as e:
            logger.info(
                "proxy_rejected_by_policy",
                from_agent=caller.get("agent_id"),
                to_agent=agent_id,
                method=method,
                reason=e.reason,
            )
            # Inc the fine-grained policy-rejection counter here in
            # the helper rather than in each of the four endpoint
            # functions: the helper is the single point where every
            # proxy path converges, so DRY-ing the inc avoids the
            # four-way drift risk that the original missing-metric
            # bug (v2 review R1) would have re-introduced anyway.
            # ``path="proxy"`` matches the dimension contract
            # documented next to the metric definition in
            # acn/monitoring/metrics.py.
            if metrics is not None:
                try:
                    await metrics.inc_counter(
                        "messages_rejected_by_policy_total",
                        labels={"path": "proxy", "reason": e.reason},
                    )
                except Exception as metric_exc:
                    # Metrics are best-effort observability — never
                    # let a Redis hiccup at counter-write time turn
                    # a clean 403 into a 500. Mirrors the same
                    # tolerance pattern in routes/communication.py.
                    #
                    # Log fields here are intentionally rich: when
                    # this warning fires, ops needs to triage
                    # "is the policy gate working but the counter
                    # backend flaky?" vs "is something more
                    # systemic broken?". Without ``from_agent`` /
                    # ``method`` it's impossible to correlate the
                    # metric-inc-failure burst against an actual
                    # misbehaving caller.
                    logger.warning(
                        "proxy_policy_metric_inc_failed",
                        from_agent=caller.get("agent_id"),
                        to_agent=agent_id,
                        method=method,
                        rest_path=rest_path or None,
                        reason=e.reason,
                        metric_error=str(metric_exc),
                    )
            raise ACNHTTPError(
                ErrorCode.COMMUNICATION_REJECTED,
                403,
                details={
                    "reason": e.reason,
                    "reject_reason": e.reject_reason,
                },
            ) from e

    real_endpoint = agent.endpoint
    if not real_endpoint:
        # ADR-0012 Mode B — the agent registered without a public HTTP
        # endpoint (e.g. a laptop / NAT'd / serverless agent). The proxy is
        # still its address: deliver in real time over its WS control
        # channel if it's connected, else fall back to the offline inbox.
        return await _relay_or_inbox(
            request=request,
            agent_id=agent_id,
            method=method,
            rest_path=rest_path,
            caller=caller,
        )

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


# ADR-0012 Mode B — seconds the proxy waits for a WS-connected agent to
# answer a relayed request. Kept under the Mode-A httpx timeout (60 s): a
# round-trip to a live agent should be quick, and a slow agent is better
# surfaced as a 504 than by pinning the caller's connection open.
_WS_RELAY_TIMEOUT_SECONDS: float = 30.0


def _request_wants_a2a_stream(body: bytes) -> bool:
    """True when the relayed body is an A2A ``message/stream`` JSON-RPC call.

    ADR-0012 P2d (#171): the gateway proxy is otherwise method-agnostic, but
    streaming needs a different relay shape (chunk frames vs a single reply),
    so we peek the JSON-RPC ``method``. Non-JSON / non-dict bodies are treated
    as non-streaming (the safe default — falls back to the single-shot path).
    """
    try:
        data = json.loads(body) if body else {}
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return isinstance(data, dict) and data.get("method") == "message/stream"


def _decode_relay_frame_payload(frame: dict) -> bytes:
    """Extract the raw bytes carried by a streaming chunk or a reply frame.

    ``a2a_stream_chunk`` carries the SSE bytes in ``data``/``data_encoding``;
    a terminal ``a2a_response`` (non-streaming handler) carries them in
    ``body``/``body_encoding``. Both decode to the bytes to write to the SSE
    response stream.
    """
    if frame.get("type") == "a2a_stream_chunk":
        raw, enc = frame.get("data") or "", frame.get("data_encoding")
    else:
        raw, enc = frame.get("body") or "", frame.get("body_encoding")
    if enc == "base64":
        try:
            return base64.b64decode(raw)
        except Exception:  # noqa: BLE001 — malformed chunk → emit nothing
            return b""
    return raw.encode("utf-8") if isinstance(raw, str) else (raw or b"")


def _relay_first_frame_to_response(ws_manager, agent_id: str, opened: tuple) -> Response:
    """Turn the first relayed reply frame into an HTTP response.

    ADR-0012 P2d (#171): mirrors the Mode-A behaviour of deciding stream vs
    buffered by what the agent ACTUALLY returns — if the first frame is a
    stream chunk, return a ``StreamingResponse`` that drains the rest until
    ``a2a_stream_end``; if it is a terminal ``a2a_response`` (the handler did
    not stream after all), buffer it into a normal ``Response``.
    """
    first, queue = opened
    correlation_id = first.get("id", "")

    if first.get("type") == "a2a_stream_chunk":
        async def _sse():
            frame = first
            try:
                while True:
                    ftype = frame.get("type")
                    if ftype == "a2a_stream_end":
                        return
                    payload = _decode_relay_frame_payload(frame)
                    if payload:
                        yield payload
                    if ftype == "a2a_response":
                        # Defensive: a terminal reply mixed into the stream.
                        return
                    frame = await asyncio.wait_for(
                        queue.get(), timeout=_WS_RELAY_TIMEOUT_SECONDS
                    )
            except (TimeoutError, asyncio.CancelledError):
                # Inter-chunk silence or client disconnect: end the SSE stream
                # (status already committed; nothing more we can signal here).
                return
            finally:
                ws_manager.close_relay_stream(correlation_id)

        return StreamingResponse(_sse(), media_type="text/event-stream")

    # Terminal a2a_response as the first frame: not actually streaming. Buffer
    # it exactly like the single-shot path and release the (unused) stream.
    ws_manager.close_relay_stream(correlation_id)
    status_code = int(first.get("status", 200))
    resp_headers = first.get("headers") or {}
    media_type = (
        resp_headers.get("content-type")
        or resp_headers.get("Content-Type")
        or "application/json"
    )
    resp_body = first.get("body") or ""
    content: bytes | str = (
        base64.b64decode(resp_body)
        if first.get("body_encoding") == "base64"
        else resp_body
    )
    return Response(content=content, status_code=status_code, media_type=media_type)


async def _relay_or_inbox(
    *,
    request: Request,
    agent_id: str,
    method: str,
    rest_path: str,
    caller: dict,
) -> Response:
    """Deliver to an agent that has no public HTTP endpoint (ADR-0012 Mode B).

    Real-time first: if the agent holds a live WebSocket control channel,
    relay the request over it and return the agent's response synchronously —
    this is the whole point of the proxy address standing in for a
    ``real_endpoint``.

    Offline backstop: with no live channel, a root A2A ``POST`` is parked in
    the agent's inbox (the same store the agent already pulls via
    ``GET /communication/inbox``) and answered ``202``; any other method
    returns ``503`` (no real-time peer, and nothing meaningful to queue).
    """
    body = await request.body()
    forward_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _PROXY_HOP_BY_HOP_HEADERS
    }
    forward_headers["X-ACN-Caller-Agent"] = caller["agent_id"]
    if caller.get("name"):
        forward_headers["X-ACN-Caller-Name"] = caller["name"]

    # WebSocketManager is a process singleton; in narrow unit-test contexts
    # it may be uninitialized — treat that as "no live channel" (fall to
    # backstop) rather than surfacing a 500.
    try:
        from .dependencies import get_ws_manager

        ws_manager = get_ws_manager()
    except Exception:  # noqa: BLE001 — uninitialized manager == offline
        ws_manager = None

    if ws_manager is not None and _request_wants_a2a_stream(body):
        # ADR-0012 P2d (#171): streaming relay. Open a stream-aware channel and
        # let the agent's first frame decide buffered vs SSE. Offline (None)
        # falls through to the shared backstop below.
        try:
            opened = await ws_manager.relay_request_open(
                agent_id,
                method=method,
                path=rest_path or "/",
                headers=forward_headers,
                body=body,
                timeout=_WS_RELAY_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning("ws_relay_stream_timeout", agent_id=agent_id, method=method)
            raise HTTPException(
                status_code=504,
                detail="Agent is connected but did not respond in time.",
            ) from None
        if opened is not None:
            logger.info("ws_relay_stream_opened", agent_id=agent_id, method=method)
            return _relay_first_frame_to_response(ws_manager, agent_id, opened)

    elif ws_manager is not None:
        try:
            relayed = await ws_manager.relay_request_to_agent(
                agent_id,
                method=method,
                path=rest_path or "/",
                headers=forward_headers,
                body=body,
                timeout=_WS_RELAY_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning("ws_relay_timeout", agent_id=agent_id, method=method)
            raise HTTPException(
                status_code=504,
                detail="Agent is connected but did not respond in time.",
            ) from None

        if relayed is not None:
            status_code = int(relayed.get("status", 200))
            resp_headers = relayed.get("headers") or {}
            media_type = (
                resp_headers.get("content-type")
                or resp_headers.get("Content-Type")
                or "application/json"
            )
            resp_body = relayed.get("body") or ""
            content: bytes | str
            if relayed.get("body_encoding") == "base64":
                content = base64.b64decode(resp_body)
            else:
                content = resp_body
            logger.info(
                "ws_relay_delivered",
                agent_id=agent_id,
                method=method,
                status=status_code,
            )
            return Response(content=content, status_code=status_code, media_type=media_type)

    # --- Offline backstop ---
    if method.upper() == "POST" and not rest_path:
        await _park_in_inbox(agent_id=agent_id, from_agent=caller["agent_id"], body=body)
        logger.info(
            "ws_relay_offline_inbox", agent_id=agent_id, from_agent=caller["agent_id"]
        )
        return Response(
            content=json.dumps(
                {
                    "status": "queued",
                    "delivery_mode": "inbox",
                    "detail": "Agent is offline; message stored in inbox for pull.",
                }
            ),
            status_code=202,
            media_type="application/json",
        )

    raise HTTPException(
        status_code=503,
        detail="Agent has no public endpoint and no active real-time connection.",
    )


async def _park_in_inbox(*, agent_id: str, from_agent: str, body: bytes) -> None:
    """Store an undeliverable A2A request in the recipient's offline inbox.

    Reuses ``MessageRouter._store_inbox`` so the entry shape, cap, and TTL
    match the existing ``/communication/send`` inbox path the recipient
    already pulls from — no second inbox format to maintain.
    """
    from .dependencies import get_router

    router = get_router()
    try:
        message: Any = json.loads(body) if body else {}
    except (json.JSONDecodeError, TypeError, ValueError):
        message = {
            "raw": body.decode("utf-8", errors="replace")
            if isinstance(body, bytes)
            else str(body)
        }
    await router._store_inbox(
        to_agent=agent_id,
        log_entry={
            "route_id": secrets.token_hex(8),
            "from_agent": from_agent,
            "to_agent": agent_id,
            "direction": "inbound",
            "timestamp": datetime.now(UTC).isoformat(),
            "delivery_mode": "inbox",
            "source": "proxy_relay_offline",
            "message": message,
        },
    )


async def _join_agent_impl(
    body: AgentJoinRequest,
    background_tasks: BackgroundTasks,
    ref: str | None,
    agent_service,
    *,
    default_metadata: dict | None = None,
) -> AgentJoinResponse:
    """Shared implementation for join_agent and join_agent_internal.

    ``default_metadata`` is merged into the agent's ``metadata`` JSONB on
    creation. Used by the internal entry point to stamp
    ``visibility="test"`` on CI/automation registrations so they do not
    pollute the public ``visibility=real`` agent list. Public registrations
    pass ``None`` and keep the legacy "no visibility tag → treated as real"
    behaviour for backwards compatibility.
    """
    try:
        referrer_id = body.referrer_id or ref

        wallet_addresses = dict(body.wallet_addresses)
        if body.wallet_address and "ethereum" not in wallet_addresses:
            wallet_addresses["ethereum"] = body.wallet_address

        # Endpoint handling keys off the delivery MODE, not merely whether a
        # URL was supplied:
        #   * Push modes (open / allowlist) deliver over HTTP, so the
        #     endpoint is load-bearing — resolve it and hard-fail on an
        #     unreachable URL (DNS / HTTP probe), same as before.
        #   * Non-pushing modes (manifest / closed) never push to the agent,
        #     so a delivery endpoint is NOT load-bearing. We store whatever
        #     direct URL was provided (already SSRF/scheme/gateway-host
        #     validated by the request validator) WITHOUT a reachability
        #     probe — probing a non-load-bearing URL would block
        #     registration for no benefit (the original closed-mode
        #     complaint). Reachability is (re)verified later via
        #     PATCH /{id}/endpoint if the agent switches to a push mode.
        _policy_mode = (body.communication_policy or {}).get("mode", "manifest")
        # ADR-0012 Mode B: relay-delivery agents are reached over their
        # outbound WebSocket (`acn listen`), never dialled — skip endpoint
        # resolution and store no direct URL even in push modes.
        if _policy_mode in _PUSH_MODES and body.delivery != "relay":
            (
                endpoint,
                agent_card,
                endpoint_reachable,
                a2a_handshake_ok,
            ) = await _resolve_registration_endpoint(
                direct_endpoint=body.get_direct_a2a_endpoint(),
                agent_card_url=body.agent_card_url,
                agent_card=body.agent_card,
            )
        else:
            endpoint = body.get_direct_a2a_endpoint()
            agent_card = body.agent_card
            # Not probed (mode doesn't push); senders consult
            # ``communication_mode`` to choose manifest-notify over direct.
            endpoint_reachable = False
            a2a_handshake_ok = None

        join_metadata = dict(default_metadata) if default_metadata else {}
        if body.self_hosted:
            # Marks the owner as the operator → rotate the key on ownership
            # change so a previous owner's extracted key is invalidated.
            join_metadata["self_hosted"] = True

        agent, api_key = await agent_service.join_agent(
            name=body.name,
            description=body.description,
            tags=body.tags,
            endpoint=endpoint,
            a2a_endpoint=endpoint,
            referrer_id=referrer_id,
            metadata=join_metadata or None,
            agent_card=agent_card,
            wallet_addresses=wallet_addresses,
            accepts_payment=body.accepts_payment,
            payment_methods=body.payment_methods,
            token_pricing=body.token_pricing,
            communication_policy=body.communication_policy,
            agent_card_url=body.agent_card_url,
            social_card_url=getattr(body, "social_card_url", None),
        )

        base_url = settings.gateway_base_url or f"http://localhost:{settings.port}"
        frontend_url = (settings.frontend_base_url or base_url).rstrip("/")

        logger.info("agent_joined", agent_id=agent.agent_id, name=agent.name, referrer_id=referrer_id)
        # System-level join event: always emit into the internal audit stream.
        # Public feeds must filter by ``public_broadcast_eligible`` so probe /
        # test registrations remain internal-only while still being auditable.
        _raw_metadata = getattr(agent, "metadata", None)
        _metadata = _raw_metadata if isinstance(_raw_metadata, dict) else {}
        _visibility = str(_metadata.get("visibility", "real")).lower()
        fire_and_forget_event(
            get_audit_singleton(),
            event_type=AuditEventType.AGENT_REGISTERED,
            actor_id=agent.agent_id,
            actor_type="agent",
            target_id=agent.agent_id,
            target_type="agent",
            details={
                "source": "join",
                "visibility": _visibility,
                "public_broadcast_eligible": _visibility == "real",
            },
        )

        background_tasks.add_task(_grant_register_reward, agent_id=agent.agent_id)

        if referrer_id:
            background_tasks.add_task(_grant_referral_reward, referrer_id=referrer_id, new_agent_id=agent.agent_id)
            background_tasks.add_task(_increment_referral_count, referrer_id=referrer_id, agent_service=agent_service)

        claim_token = agent.verification_code or ""
        # Token is base64-url alphabet (``secrets.token_urlsafe``) so it
        # never embeds reserved characters, but go through ``quote`` anyway
        # — defensive against any future change to the token alphabet, and
        # documents the intent that this value lands in a URL query string.
        # The route shape MUST match the frontend page
        # (``agentplanet/frontend/src/app/claim/[id]/page.tsx`` reads
        # ``searchParams.get("token")``). The earlier path-segment shape
        # ``/claim/{id}/{token}`` only ever rendered the Next.js 404 page
        # because the dynamic route has a single ``[id]`` segment.
        claim_url = f"{frontend_url}/claim/{agent.agent_id}?token={quote(claim_token, safe='')}"
        _mode = (agent.communication_policy or {}).get("mode", "manifest")
        _hint = _build_next_step_hint(
            mode=_mode,
            has_endpoint=endpoint is not None,
            endpoint_reachable=endpoint_reachable,
            a2a_handshake_ok=a2a_handshake_ok,
            agent_id=agent.agent_id,
            base_url=base_url,
        )
        return AgentJoinResponse(
            agent_id=agent.agent_id,
            api_key=api_key,
            status="online" if await agent_service.is_alive(agent.agent_id) else "offline",
            claim_status=agent.claim_status.value if agent.claim_status else "unclaimed",
            verification_code=claim_token,
            claim_url=claim_url,
            referral_url=f"{base_url}/api/v1/agents/join?ref={agent.agent_id}",
            tasks_endpoint=f"{base_url}/api/v1/tasks",
            heartbeat_endpoint=f"{base_url}/api/v1/agents/{agent.agent_id}/heartbeat",
            agent_card_url=f"{base_url}/api/v1/agents/{agent.agent_id}/.well-known/agent-card.json",
            endpoint_reachable=endpoint_reachable,
            a2a_handshake_ok=a2a_handshake_ok,
            communication_mode=_mode,
            next_step_hint=_hint,
        )
    except ACNHTTPError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error("agent_join_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Agent join failed") from e


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
        raise ACNHTTPError(
            ErrorCode.INTERNAL_TOKEN_INVALID,
            401,
            message="The X-Internal-Token header is missing or does not match.",
        )
    # Get agent service manually to avoid FastAPI Depends() injection edge cases
    from .dependencies import get_agent_service as _get_svc
    try:
        agent_svc = _get_svc()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail="Service temporarily unavailable") from e
    # Internal callers are CI / smoke tests / operator scripts. Stamp
    # ``visibility=test`` so these registrations are excluded from the
    # default public agent list and never surface on /world.
    return await _join_agent_impl(
        body,
        background_tasks,
        ref,
        agent_svc,
        default_metadata={"visibility": "test"},
    )


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
# L418: per-wallet ceiling on top of per-agent. Proxy traffic is the
# highest-volume inbound surface (every A2A JSON-RPC call lands here
# by default), so without the wallet bucket the multi-account abuse
# pattern would simply migrate from /communication/send onto the
# proxy and recover full leverage.
@limiter.limit(WALLET_RATE_LIMIT, key_func=_wallet_rate_limit_key)
async def proxy_post(
    request: Request,
    agent_id: AgentIdPath,
    caller: ProxyCallerDep,
    agent_service: AgentServiceDep = None,
    policy_service: PolicyServiceDep = None,
    metrics: MetricsDep = None,
):
    """Proxy POST to agent's real endpoint — A2A JSON-RPC (message/send, message/stream, tasks/*).

    Requires ``X-ACN-Authorization: Bearer <ACN_API_KEY>`` to identify the calling
    agent; rate-limit is bucketed per-agent.

    Subject to recipient ``communication_policy`` — a ``closed`` recipient
    returns HTTP 403 ``communication_rejected`` instead of forwarding,
    and increments ``acn_messages_rejected_by_policy_total{path="proxy"}``.
    """
    return await _proxy_to_agent(
        request, agent_id, "POST", "", agent_service, caller, policy_service, metrics
    )


@router.put("/{agent_id}")
@limiter.limit("60/minute")
@limiter.limit(WALLET_RATE_LIMIT, key_func=_wallet_rate_limit_key)
async def proxy_put(
    request: Request,
    agent_id: AgentIdPath,
    caller: ProxyCallerDep,
    agent_service: AgentServiceDep = None,
    policy_service: PolicyServiceDep = None,
    metrics: MetricsDep = None,
):
    """Proxy PUT to agent's real endpoint. Requires ``X-ACN-Authorization``.
    Subject to recipient ``communication_policy`` (see proxy_post)."""
    return await _proxy_to_agent(
        request, agent_id, "PUT", "", agent_service, caller, policy_service, metrics
    )


@router.patch("/{agent_id}")
@limiter.limit("60/minute")
@limiter.limit(WALLET_RATE_LIMIT, key_func=_wallet_rate_limit_key)
async def proxy_patch(
    request: Request,
    agent_id: AgentIdPath,
    caller: ProxyCallerDep,
    agent_service: AgentServiceDep = None,
    policy_service: PolicyServiceDep = None,
    metrics: MetricsDep = None,
):
    """Proxy PATCH to agent's real endpoint. Requires ``X-ACN-Authorization``.
    Subject to recipient ``communication_policy`` (see proxy_post)."""
    return await _proxy_to_agent(
        request, agent_id, "PATCH", "", agent_service, caller, policy_service, metrics
    )


_VISIBILITY_VALUES = frozenset({"real", "hidden", "spam", "archived", "all"})


_AGENTS_MAX_LIMIT = 500


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
    visibility: str = Query(
        default="real",
        description=(
            "Data-hygiene filter on metadata.visibility. "
            "'real' (default) shows only production agents. "
            "'all' returns every registered agent regardless of visibility. "
            "Other values: hidden (internal bots), spam, archived. "
            "Agents without an explicit visibility tag are treated as 'real'."
        ),
    ),
    owner: str | None = None,
    name: str | None = None,
    subnet: str | None = Query(
        default=None,
        description="Filter by subnet slug — only agents that are members of this subnet are returned.",
    ),
    limit: int = Query(
        default=200,
        ge=1,
        le=_AGENTS_MAX_LIMIT,
        description=f"Maximum number of agents to return (1–{_AGENTS_MAX_LIMIT}).",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Zero-based offset for pagination.",
    ),
    credentials: HTTPAuthorizationCredentials | None = Security(_optional_bearer),
    agent_service: AgentServiceDep = None,
    subnet_service: SubnetServiceDep = None,
):
    """Search agents.

    Clean Architecture: Route → AgentService → Repository

    ``subnet_ids`` visibility (ACL V6 B3): admin callers receive the
    full list per agent. All other callers (including unauthenticated)
    see only public subnet slugs per row. Agent self-listing is not
    applicable on the list endpoint — use ``GET /agents/me`` for full
    self-view.
    """
    if visibility not in _VISIBILITY_VALUES:
        raise HTTPException(
            status_code=422,
            detail=f"visibility must be one of: {', '.join(sorted(_VISIBILITY_VALUES))}",
        )

    # Resolve optional caller payload for subnet_ids ACL.
    caller_payload: dict | None = None
    if credentials:
        try:
            caller_payload = await verify_token(request, credentials)
        except Exception:  # noqa: BLE001 — invalid token → treat as anon
            caller_payload = None

    # On the list endpoint "self" semantics don't apply — check admin only.
    # Public slugs are fetched once and reused for every row.
    is_admin = caller_payload is not None and "acn:admin" in caller_payload.get(
        "permissions", []
    )
    public_slugs: set[str] | None = None if is_admin else await _get_public_subnet_slugs(
        subnet_service
    )

    # P3-5 / P2-4: non-admin callers are restricted to visibility="real".
    # Allowing arbitrary visibility values to anonymous callers leaks the
    # existence of hidden/archived/spam agents.
    if not is_admin and visibility != "real":
        visibility = "real"

    # ACL: if ?subnet= is given, verify the subnet exists and that the caller
    # is allowed to enumerate its members. Private subnets are only queryable
    # by members (via agent API key) or admins.
    if subnet:
        subnet_entity = await subnet_service.get_subnet(subnet)
        if subnet_entity is None:
            raise ACNHTTPError(ErrorCode.SUBNET_NOT_FOUND, 404, details={"slug": subnet})
        if subnet_entity.is_private and not is_admin:
            caller_agent_id: str | None = None
            if credentials:
                try:
                    caller_agent = await agent_service.get_agent_by_api_key(
                        credentials.credentials
                    )
                    caller_agent_id = caller_agent.agent_id if caller_agent else None
                except Exception:  # noqa: BLE001
                    caller_agent_id = None
            if caller_agent_id is None or not subnet_entity.has_member(caller_agent_id):
                raise ACNHTTPError(
                    ErrorCode.NOT_SUBNET_MEMBER,
                    403,
                    details={"slug": subnet},
                )

    tag_param = tag or skill  # accept both; `tag` takes precedence
    tag_list = tag_param.split(",") if tag_param else None

    # Search using AgentService
    agents = await agent_service.search_agents(
        tags=tag_list,
        status=status,
        slug=subnet,
    )

    # Apply visibility hygiene filter.
    # Agents without metadata.visibility are treated as "real" (open-world
    # assumption: all pre-existing agents are real unless explicitly marked).
    if visibility != "all":
        agents = [
            a for a in agents
            if (a.metadata or {}).get("visibility", "real") == visibility
        ]

    # Apply additional filters (owner, name)
    if owner:
        agents = [a for a in agents if a.owner == owner]
    if name:
        agents = [a for a in agents if name.lower() in a.name.lower()]

    total_matched = len(agents)

    # Apply pagination window before the expensive per-agent alive check +
    # model serialization. This bounds peak memory to O(limit) regardless of
    # total corpus size.
    agents = agents[offset : offset + limit]

    agent_infos = await _agent_entities_to_infos(
        agents,
        agent_service=agent_service,
        strip_sensitive=True,
        public_subnet_slugs=public_slugs,
    )

    return AgentSearchResponse(
        agents=agent_infos,
        total=total_matched,
        limit=limit,
        offset=offset,
    )


@router.post("/{agent_id}/heartbeat")
@limiter.limit("30/minute")
async def agent_heartbeat(
    request: Request,
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
        raise ACNHTTPError(
            ErrorCode.API_KEY_AGENT_MISMATCH,
            403,
            details={
                "path_agent": agent_id,
                "key_agent": agent_info["agent_id"],
            },
        )
    try:
        await agent_service.update_heartbeat(agent_id)
        return {"status": "ok", "agent_id": agent_id}
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e


def _acn_proxy_url_for(agent_id: str) -> str:
    """Return the canonical ACN-proxy URL for an agent.

    This is the URL the public agent card must advertise instead of
    the agent's real backend endpoint — every inbound request must
    transit ACN so ``communication_policy`` (and future rate
    limiting / billing) gates can run. Centralizing the format here
    keeps it in lockstep with ``_agent_entity_to_info`` (which
    already rewrites ``endpoint`` for the same reason).
    """
    base_url = settings.gateway_base_url or f"http://localhost:{settings.port}"
    return f"{base_url}/api/v1/agents/{agent_id}"


@router.get("/{agent_id}/.well-known/agent-card.json")
# Phase 1 integration review (P1-1): public, unauthenticated endpoint
# — required by A2A discovery, so we can't gate it behind auth, but
# without ANY rate limit it lets an attacker enumerate every agent_id
# in O(N) DB reads with zero cost. 60/minute per IP key (the
# unauthenticated fallback in ``_rate_limit_key``) keeps honest
# discovery traffic comfortably under cap (one card fetch + 60s
# client cache is the typical pattern) while throttling enumerators.
@limiter.limit("60/minute")
async def get_agent_card(
    request: Request,
    agent_id: AgentIdPath,
    agent_service: AgentServiceDep = None,
):
    """Get agent's A2A Agent Card (v0.3.0 compliant)

    Returns the card submitted at registration time if available.
    Falls back to auto-generating a minimal card from stored fields.

    Phase 1 L422: the *public* card is sanitized so that the
    top-level ``url`` advertises the **ACN proxy address**, not
    the agent's real backend endpoint. Otherwise an attacker could
    skip the entire ACN gate (proxy / router / subnet_manager) by
    pulling the well-known card and dialing the backend directly,
    nullifying every ``communication_policy`` decision.

    Phase 1 deliberately stops at the top-level ``url`` — extension
    fields (``services[]``, ``additionalInterfaces``, etc.) are not
    deep-walked. Phase 2 adds field-level sanitization once we
    survey what third-party cards actually embed (see
    docs/features/acn-communication-economic-model.md L433).
    """
    try:
        agent = await agent_service.get_agent(agent_id)

        proxy_url = _acn_proxy_url_for(agent_id)

        # Path A: caller-supplied card (e.g. OpenPersona-generated).
        # Repositories typically hand back a fresh dict, but we
        # don't want to take that on faith — a shared reference
        # would let a future mutation leak the rewritten URL into
        # the cached entity. Shallow-copy the top-level dict and
        # overwrite ``url``; nested fields (``services[]`` etc.)
        # are out of scope for Phase 1 (see docstring above).
        if agent.agent_card:
            sanitized = dict(agent.agent_card)
            sanitized["url"] = proxy_url
            return sanitized

        # Path B: fallback auto-generated card. Build it directly
        # against the proxy URL — never persist or expose the real
        # endpoint here.
        card = AgentCard(
            name=agent.name,
            version="0.1.0",
            description=agent.description or f"{agent.name} on ACN",
            url=proxy_url,
            capabilities=AgentCapabilities(streaming=False),
            default_input_modes=["text", "application/json"],
            default_output_modes=["text", "application/json"],
            skills=[
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
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e


@router.get("/{agent_id}/.well-known/agent-registration.json")
@limiter.limit("60/minute")
async def get_agent_registration_file(
    request: Request,
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
        is_online = await agent_service.is_alive(agent_id)
        return build_erc8004_registration_file(agent, cfg, is_online=is_online)
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e


@router.get("/{agent_id}/endpoint")
# Phase 1 integration review (P1-1): owner-or-internal authenticated,
# but every successful read writes an INFO ``agent_endpoint_disclosed``
# audit log. Without a per-agent rate cap, a leaked owner API key
# could be looped to drown the audit stream and obscure subsequent
# attacks. 60/minute is far above any legitimate self-introspection
# pattern (humans + scripts alike).
@limiter.limit("60/minute")
async def get_agent_endpoint(
    request: Request,
    agent_id: AgentIdPath,
    caller: OwnerOrInternalDep,
    agent_service: AgentServiceDep = None,
):
    """Return the agent's real backend endpoint URL.

    Auth (Phase 1 L421): the response leaks the *one* piece of data
    the ACN proxy was designed to hide. A caller who has it can
    reach the agent without ever entering ACN, defeating every
    ``communication_policy`` gate (proxy / router / subnet_manager).
    Therefore we restrict access to:

      - X-Internal-Token (ops / platform), OR
      - Authorization: Bearer <API_KEY> matching ``agent_id``
        (the agent introspecting its own endpoint).

    Anyone else gets 401/403 — there's no anonymous read path
    for the real endpoint anymore. Public agent metadata stays
    available via the ACN-proxy-rewritten ``GET /agents/{id}``
    response.

    Clean Architecture: Route → AgentService → Repository.
    """
    try:
        agent = await agent_service.get_agent(agent_id)
        # Audit the access at INFO level — knowing who pulled an
        # agent's real endpoint is high signal for incident
        # response (e.g. correlating a leaked-token incident with
        # subsequent direct calls to the agent's backend).
        logger.info(
            "agent_endpoint_disclosed",
            agent_id=agent_id,
            caller_kind=caller.get("caller_kind"),
            caller_agent_id=caller.get("agent_id"),
        )
        return {"agent_id": agent_id, "endpoint": agent.endpoint}
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e


class EndpointPatchRequest(BaseModel):
    """PATCH body for ``/agents/{id}/endpoint``.

    Wraps the URL in a top-level ``endpoint`` key, mirroring the field
    name everywhere else. This is the pull→push upgrade path: a
    manifest-mode agent that later stands up an HTTPS server registers
    it here instead of re-joining (which would mint a new ``agent_id``).
    Pass ``null`` to clear the endpoint and revert to pull-only.
    """

    endpoint: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "New direct A2A delivery URL, or null to clear (revert to "
            "pull-only). Must be an http(s) URL on the agent's own host "
            "(not the ACN gateway) and pass SSRF checks."
        ),
    )

    @field_validator("endpoint")
    @classmethod
    def _validate(cls, v: str | None) -> str | None:
        return _validate_agent_endpoint_url(v)


@router.patch("/{agent_id}/endpoint")
# Same per-agent ceiling as the policy / social-card PATCH endpoints:
# writes DB + invalidates cache + (on set) fires an outbound reachability
# probe, so a leaked owner key spamming it could push both the audit
# stream and external probe traffic. 30/min is ~one update per 2s, far
# above any legitimate operator pattern.
@limiter.limit("30/minute")
async def update_agent_endpoint(
    request: Request,
    agent_id: AgentIdPath,
    body: EndpointPatchRequest,
    caller: OwnerOrInternalDep,
    agent_service: AgentServiceDep = None,
):
    """Set or clear an agent's delivery endpoint after registration.

    The pull→push upgrade path. A manifest-mode agent that deploys an
    HTTPS server calls this to start receiving direct delivery, without
    re-registering (which would issue a new ``agent_id`` and orphan its
    identity, reputation, and subnet memberships).

    Auth: ``OwnerOrInternalDep`` — same as ``GET /{id}/endpoint`` and
    ``PATCH /{id}/policy``. The agent (Bearer API key) manages its own
    endpoint; X-Internal-Token covers ops.

    Setting an endpoint runs the same two-layer reachability check as
    registration (DNS hard-fail + HTTP probe hard-fail → 400) so a dead
    URL can't silently black-hole inbound delivery. Clearing
    (``endpoint=null``) is rejected when the agent is in a push mode
    (``open`` / ``allowlist``) because that would leave it advertising a
    delivery mode with nowhere to deliver — switch to ``manifest`` /
    ``closed`` via ``PATCH /{id}/policy`` first.
    """
    try:
        agent = await agent_service.get_agent(agent_id)
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e

    current_mode = (agent.communication_policy or {}).get("mode", "open")

    endpoint_reachable = False
    a2a_handshake_ok: bool | None = None
    if body.endpoint is not None:
        # Two-layer check identical to registration: DNS + HTTP probe,
        # both hard-fail with 400. Scheme / gateway-host / SSRF were
        # already enforced by the request validator.
        endpoint_reachable = await _check_endpoint_reachability(body.endpoint)
        # Soft A2A handshake probe — never blocks. Catches the pull→push
        # upgrade case where the operator points the endpoint at a bare origin
        # while their A2A server is mounted at /a2a (would silently 404).
        a2a_handshake_ok = await _probe_a2a_handshake(body.endpoint)
    else:
        # Clearing while in a push mode would leave the agent in the
        # exact inconsistent state the registration validator forbids.
        if current_mode in _PUSH_MODES:
            raise ACNHTTPError(
                ErrorCode.INVALID_REQUEST,
                400,
                message=(
                    f"Cannot clear the endpoint while communication_policy.mode="
                    f"{current_mode!r}: that mode pushes messages over HTTP. "
                    "Switch to 'manifest' or 'closed' via PATCH /agents/{id}/policy "
                    "first, then clear the endpoint."
                ),
                details={"reason": "endpoint_required_for_mode", "mode": current_mode},
            )

    updated = await agent_service.update_endpoint(
        agent_id=agent_id,
        endpoint=body.endpoint,
    )

    logger.info(
        "agent_endpoint_updated",
        agent_id=agent_id,
        caller_kind=caller.get("caller_kind"),
        caller_agent_id=caller.get("agent_id"),
        endpoint_set=updated.endpoint is not None,
        endpoint_reachable=endpoint_reachable,
        a2a_handshake_ok=a2a_handshake_ok,
    )

    return {
        "agent_id": agent_id,
        "endpoint": updated.endpoint,
        "endpoint_reachable": endpoint_reachable,
        "a2a_handshake_ok": a2a_handshake_ok,
        "communication_mode": current_mode,
    }


# ---------------------------------------------------------------------------
# Phase 1 L410-B: PATCH /api/v1/agents/{id}/policy
# ---------------------------------------------------------------------------
#
# The user-facing knob for ``communication_policy``. Before this
# existed, the field was reachable only at registration time —
# meaning every already-registered agent was effectively pinned to
# whatever they chose (or the default ``open``) with no migration
# path short of re-registration. That made the entire ``closed``
# capability **unreachable in production** for the existing agent
# population, defeating the point of shipping it.
#
# Auth: ``OwnerOrInternalDep`` — same shape as
# ``GET /{id}/endpoint``. Reasoning:
#   - The agent itself (Bearer API key) toggling its own policy is
#     the legitimate user flow — pinning to a more privileged
#     identity (e.g. Auth0 owner JWT) would lock out CLI / scripted
#     agents that authenticate via API key.
#   - X-Internal-Token covers ops scenarios (e.g. emergency forced
#     close on a misbehaving agent reported by abuse).
class CommunicationPolicyPatchRequest(BaseModel):
    """PATCH body for ``/agents/{id}/policy``.

    Wraps the policy in a top-level ``communication_policy`` key so
    the body shape mirrors the shape it has in
    ``AgentRegisterRequest`` / ``AgentJoinRequest`` — same
    validator, same error messages, same JSON path. ``None`` is
    accepted as an explicit "reset to default open" signal so
    operators have a way to clear a stuck custom policy.
    """

    communication_policy: dict | None = Field(
        default=None,
        description=(
            "New inbound message policy. Phase 1 accepts "
            "{'mode': 'open' | 'closed', 'reject_reason'?: str}. "
            "Pass null to reset to the default open policy."
        ),
    )

    @field_validator("communication_policy")
    @classmethod
    def validate_communication_policy(cls, v):
        from ..services.policy_service import validate_policy_dict

        return validate_policy_dict(v)


@router.get(
    "/{agent_id}/policy",
    # Auth dependency mounted via ``dependencies=[...]`` rather
    # than as a function argument: the read path doesn't need
    # ``caller`` for any logging / audit signal (unlike PATCH and
    # ``GET /endpoint`` which both record the principal). Owners
    # frequently poll their own policy, so adding an INFO log per
    # GET would just generate noise without forensic value, and
    # cross-tenant reads are already blocked by the auth gate
    # itself (a 403 attempt is recorded by ``_record_auth_failure``).
    # Mounting via ``dependencies=`` keeps the signature minimal
    # while preserving the same auth semantics as PATCH.
    dependencies=[Depends(verify_owner_or_internal)],
)
# Phase 1 integration review (P1-1): same per-agent ceiling as
# ``GET /endpoint``. Owners are expected to poll their own policy
# (e.g. dashboard refreshes), so 60/minute is generous; the cap is
# there to bound a leaked-key replay loop, not legitimate polling.
@limiter.limit("60/minute")
async def get_agent_policy(
    request: Request,
    agent_id: AgentIdPath,
    agent_service: AgentServiceDep = None,
):
    """Read an agent's current ``communication_policy``.

    Symmetric counterpart to ``PATCH /{id}/policy``. Required
    because ``AgentInfo.communication_policy`` is ``exclude=True``
    (so the public ``GET /agents/{id}`` deliberately doesn't echo
    the field — that's the right call for the public surface, but
    leaves owners with no way to introspect their own policy).
    Without this read endpoint, the only way an owner could check
    "what's my current policy?" would be to issue a redundant
    PATCH and read the response — clumsy and racy.

    Auth matches PATCH (``verify_owner_or_internal``): policy is
    *internal* configuration, not public metadata; only the owner
    or platform tooling needs to see ``reject_reason`` (which can
    be free-form and may carry sensitive context like a medical-
    leave date).
    """
    try:
        agent = await agent_service.get_agent(agent_id)
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e

    # ``Agent.__post_init__`` backfills ``{"mode": "open"}`` on
    # entities that predate the field, so the response shape is
    # always a non-null dict — clients don't need to handle the
    # ``None`` case.
    return {
        "agent_id": agent_id,
        "communication_policy": agent.communication_policy or {"mode": "open"},
    }


@router.get("/{agent_id}/communication_profile")
@limiter.limit("120/minute")
async def get_communication_profile(
    request: Request,
    agent_id: AgentIdPath,
    agent_service: AgentServiceDep = None,
    manifest_service: ManifestServiceDep = None,
):
    """Public read-only summary of an agent's communication policy.

    Exposes only the fields a *sender* needs before deciding whether
    to attach an ``attention_fee`` or whether a message will be
    accepted at all. Intentionally omits ``reject_reason`` (which
    may contain sensitive context) and the full allowlist membership.

    Returns:
        ``{"agent_id": ..., "mode": ..., "attention_fee_required": bool,
          "unread_manifest_count": int}``
        ``mode`` is one of ``open | manifest | allowlist | closed``.
        ``attention_fee_required`` is ``true`` when the policy carries
        an ``attention_fee`` requirement (reserved for future use;
        currently always ``false`` — fee is optional at sender side).
        ``unread_manifest_count`` shows the number of pending manifest
        entries — useful for monitoring queue buildup when the agent
        has no active polling.
    """
    try:
        agent = await agent_service.get_agent(agent_id)
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e

    policy = agent.communication_policy or {"mode": "open"}

    unread_count = 0
    if manifest_service is not None:
        try:
            entries = await manifest_service.read_since(agent_id, limit=200)
            unread_count = len([e for e in entries if e.acked_at_ms is None])
        except Exception:
            pass

    return {
        "agent_id": agent_id,
        "mode": policy.get("mode", "open"),
        "attention_fee_required": bool(policy.get("attention_fee_required", False)),
        "unread_manifest_count": unread_count,
    }


@router.patch("/{agent_id}/policy")
# Phase 1 integration review (P1-1): tighter cap than the read paths
# because PATCH is destructive — it writes DB (Postgres UPDATE +
# Redis SET), invalidates the agent cache, and emits a structured
# INFO ``communication_policy_updated`` log. A leaked owner API key
# spamming PATCH could push the cache + audit stream hard. 30/minute
# leaves ~one toggle per 2s, which is two orders of magnitude above
# any legitimate operator pattern (humans rarely flip policy more
# than a few times a day, automation a handful of times per minute
# during incident response).
@limiter.limit("30/minute")
async def update_agent_policy(
    request: Request,
    response: Response,
    agent_id: AgentIdPath,
    body: CommunicationPolicyPatchRequest,
    caller: OwnerOrInternalDep,
    agent_service: AgentServiceDep = None,
):
    """Update an agent's ``communication_policy``.

    See ``CommunicationPolicyPatchRequest`` for the accepted shape.
    Returns the post-update policy so the caller can confirm the
    persisted value (especially useful when passing ``null`` to
    reset, since the response shows the entity-layer default).
    """
    # Phase 2 PR #1 (Group B #3-bis): we now need ``old_mode`` for
    # the structured audit event, so capture it before the update.
    # Wrap in get_agent so the 404 path still wins over any audit
    # work — a missing agent should not surface a "policy_changed"
    # audit event with a phantom old_mode.
    try:
        existing = await agent_service.get_agent(agent_id)
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e
    old_mode = (existing.communication_policy or {}).get("mode", "open")

    try:
        agent = await agent_service.update_communication_policy(
            agent_id=agent_id,
            communication_policy=body.communication_policy,
        )
    except AgentNotFoundException as e:
        # Race: agent was deleted between get_agent and update. Same
        # surface (404) as the pre-check; just keep the audit emit
        # path inert.
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e

    new_mode = (agent.communication_policy or {}).get("mode", "open")

    # Phase 2 PR #1 promotes the historical INFO log to a structured
    # POLICY_CHANGED audit event so platform tooling can correlate
    # policy flips with subsequent traffic drops in MESSAGE_REJECTED.
    # We keep it best-effort (fire_and_forget) — the policy mutation
    # is already persisted; failing to write the audit must not 5xx
    # the request. Sensitive transitions (open -> closed/manifest)
    # ALSO get a structured WARNING log so on-call notices even
    # when the audit pipeline is degraded.
    sensitive = old_mode == "open" and new_mode in ("closed", "manifest")
    fire_and_forget_event(
        get_audit_singleton(),
        event_type=AuditEventType.POLICY_CHANGED,
        actor_id=caller.get("agent_id"),
        actor_type=caller.get("caller_kind"),
        target_id=agent_id,
        target_type="agent",
        level=AuditLevel.WARNING if sensitive else AuditLevel.INFO,
        details={
            "old_mode": old_mode,
            "new_mode": new_mode,
            "caller_kind": caller.get("caller_kind"),
        },
    )
    if sensitive:
        logger.warning(
            "communication_policy_tightened",
            agent_id=agent_id,
            old_mode=old_mode,
            new_mode=new_mode,
            caller_kind=caller.get("caller_kind"),
            caller_agent_id=caller.get("agent_id"),
        )
    else:
        logger.info(
            "communication_policy_updated",
            agent_id=agent_id,
            caller_kind=caller.get("caller_kind"),
            caller_agent_id=caller.get("agent_id"),
            old_mode=old_mode,
            new_mode=new_mode,
        )

    # Phase 2 review v2 P1 #10: emit ``X-ACN-SDK-Min-Version`` whenever
    # the post-update mode requires an SDK that understands
    # ``manifest_notification`` (manifest mode) or polls the manifest
    # queue for allowlist non-member fan-outs. Old clients without
    # those handlers won't see any of the upcoming traffic — surfacing
    # the requirement as a response header lets dashboards / SDK
    # release tooling pick it up automatically (compared to documenting
    # it only in release notes, which gets missed).
    #
    # Header is emitted on EVERY PATCH that resolves to one of the
    # gated modes — including no-op ``manifest -> manifest``
    # idempotency. See ``_MODES_REQUIRING_SDK_NOTIFY`` for the full
    # rationale on always-emit vs transition-only emit.
    if new_mode in _MODES_REQUIRING_SDK_NOTIFY:
        response.headers[_SDK_MIN_VERSION_HEADER] = (
            settings.policy_manifest_min_sdk_version
        )

    result: dict[str, Any] = {
        "agent_id": agent_id,
        "communication_policy": agent.communication_policy,
    }
    if new_mode in ("manifest", "allowlist"):
        result["warning"] = (
            "Messages from non-trusted senders will be diverted to the manifest "
            "queue. Your agent must periodically poll GET /communication/manifest/{id} "
            "to receive them. Without active polling, these messages are unreachable "
            "and will expire after the configured TTL (default 7 days)."
        )
    return result


# ---------------------------------------------------------------------------
# PATCH /api/v1/agents/{id}/social-card-url
# ---------------------------------------------------------------------------
#
# User-facing knob for the SOCIAL.md pointer. We intentionally don't piggy-
# back on the registration endpoint for two reasons:
#
#   1. SOCIAL.md is a separate concern from registration metadata. An owner
#      might publish their SOCIAL.md weeks after the agent first joins
#      (especially relevant during the spec's bootstrapping phase).
#   2. A dedicated endpoint matches the policy-PATCH pattern, which keeps
#      ops/audit tooling uniform: every "owner mutates one configuration
#      knob" event has the same shape.
#
# Auth: ``verify_owner_or_internal`` (same as policy PATCH).
#   - Owner can update their own agent's URL.
#   - X-Internal-Token covers ops scenarios (e.g. revoking a stale URL after
#     an agent operator's domain has been hijacked).
#
# Validation:
#   - Pydantic field validator strips whitespace, enforces https/http prefix
#     and the 2048-char cap (mirrors AgentInfo / AgentRegisterRequest).
#   - Empty string is normalized to None (clears the field).
#
# What this endpoint does NOT do:
#   - Fetch the URL. The body lives at the URL and consumers fetch on
#     demand per https://agentsocial.one/consumption-model. Validating the
#     body here would (a) couple ACN to the SOCIAL.md spec version, (b)
#     create an SSRF surface, and (c) duplicate work consumers must do
#     anyway.
#   - Mirror the body anywhere. ACN holds only the pointer.
class SocialCardUrlPatchRequest(BaseModel):
    """PATCH body for ``/agents/{id}/social-card-url``.

    Wraps the URL in a top-level ``social_card_url`` key so the body
    shape mirrors the field name everywhere else (``Agent``,
    ``AgentInfo``, ``AgentRegisterRequest``). Pass ``null`` to clear.
    """

    social_card_url: str | None = Field(
        default=None,
        max_length=2048,
        description=(
            "URL to the agent's SOCIAL.md, or null to clear. Must start "
            "with https:// (or http:// in dev)."
        ),
    )

    @field_validator("social_card_url")
    @classmethod
    def validate_social_card_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if not (v.lower().startswith("https://") or v.lower().startswith("http://")):
            raise ValueError("social_card_url must start with https:// or http://")
        return v


@router.patch("/{agent_id}/social-card-url")
# Same per-agent rate limit ceiling as the policy PATCH: destructive-ish
# (writes DB + invalidates cache + emits audit log) and a leaked owner
# key spamming PATCH would push the cache + audit stream hard. 30/min
# leaves ~one update per 2s, two orders of magnitude above any
# legitimate operator pattern (humans rarely change their published
# SOCIAL.md URL more than a handful of times).
@limiter.limit("30/minute")
async def update_agent_social_card_url(
    request: Request,
    agent_id: AgentIdPath,
    body: SocialCardUrlPatchRequest,
    caller: OwnerOrInternalDep,
    agent_service: AgentServiceDep = None,
):
    """Update an agent's ``social_card_url`` pointer.

    See ``SocialCardUrlPatchRequest`` for the accepted shape. Returns
    the post-update URL so the caller can confirm the persisted value
    (especially useful when passing ``null`` to clear, since the
    response shows the cleared state).
    """
    try:
        agent = await agent_service.update_social_card_url(
            agent_id=agent_id,
            social_card_url=body.social_card_url,
        )
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e

    # Log every URL mutation. SOCIAL.md content can change semantics
    # (mode flip from open → closed, fee changes, retention policy
    # changes) without the URL changing — but the URL itself
    # changing means the agent has switched their entire social
    # identity surface, which is worth tracking for forensic /
    # anti-impersonation purposes. Same INFO-log-not-audit choice as
    # the policy endpoint: low cardinality at expected operator rates.
    logger.info(
        "social_card_url_updated",
        agent_id=agent_id,
        caller_kind=caller.get("caller_kind"),
        caller_agent_id=caller.get("agent_id"),
        new_url=agent.social_card_url,
    )

    return {
        "agent_id": agent_id,
        "social_card_url": agent.social_card_url,
    }


class ProfilePatchRequest(BaseModel):
    """PATCH body for ``/agents/{id}/profile``.

    Partial update of editable metadata: ``name`` / ``description`` /
    ``tags``. Every field is optional — only those present are changed.
    This is a PATCH (partial), not a PUT (replace): omitting a field
    leaves it untouched; it is never blanked out. ``tags`` *is* replaced
    wholesale when present (the list is the unit of update); pass the
    full desired list, or ``[]`` to clear all tags.

    The same validators that run at registration apply here so a value
    rejected on join can't slip in via edit.
    """

    name: str | None = Field(
        default=None,
        min_length=2,
        max_length=100,
        description="New display name, or omit to leave unchanged.",
    )
    description: str | None = Field(
        default=None,
        min_length=10,
        max_length=500,
        description="New description, or omit to leave unchanged.",
    )
    tags: list[str] | None = Field(
        default=None,
        max_length=20,
        description=(
            "New full capability-tag list (replaces the existing list), "
            "or omit to leave unchanged. Pass [] to clear all tags."
        ),
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _validate_agent_name(v)

    @model_validator(mode="after")
    def _require_at_least_one_field(self):
        if self.name is None and self.description is None and self.tags is None:
            raise ValueError(
                "At least one of name, description, or tags must be provided."
            )
        return self


@router.patch("/{agent_id}/profile")
# Same per-agent ceiling as the policy / social-card / endpoint PATCH
# endpoints: writes DB + invalidates cache. A leaked owner key spamming
# it could churn the cache and audit stream. 30/min is ~one edit per 2s,
# far above any legitimate operator pattern.
@limiter.limit("30/minute")
async def update_agent_profile(
    request: Request,
    agent_id: AgentIdPath,
    body: ProfilePatchRequest,
    caller: OwnerOrInternalDep,
    agent_service: AgentServiceDep = None,
):
    """Partial update of an agent's editable metadata (name/description/tags).

    Closes the "agents can't edit their own basic info after registration"
    gap: previously ``name`` / ``description`` / ``tags`` were fixed at
    join time, and ``PATCH /{id}`` is the A2A proxy, not a metadata route.

    Auth: ``OwnerOrInternalDep`` — the agent itself (Bearer API key) or
    X-Internal-Token, identical to ``PATCH /{id}/policy`` and
    ``/social-card-url``. Only fields present in the body are changed.
    """
    try:
        updated = await agent_service.update_profile(
            agent_id=agent_id,
            name=body.name,
            description=body.description,
            tags=body.tags,
        )
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e

    # M3: the auth cache rows carry the agent name, so a name change must
    # evict the stale entry or downstream callers would see the old name
    # for up to the cache TTL.
    evict_agent_from_cache(agent_id)

    logger.info(
        "agent_profile_updated",
        agent_id=agent_id,
        caller_kind=caller.get("caller_kind"),
        caller_agent_id=caller.get("agent_id"),
        fields=[
            f
            for f, v in (
                ("name", body.name),
                ("description", body.description),
                ("tags", body.tags),
            )
            if v is not None
        ],
    )

    return {
        "agent_id": agent_id,
        "name": updated.name,
        "description": updated.description,
        "tags": updated.tags,
    }


@router.delete("")
async def admin_bulk_delete_agents(
    request: Request,
    _: InternalTokenDep,
    agent_service: AgentServiceDep = None,
    name_prefix: str | None = None,
    owner: str | None = None,
    agent_ids: str | None = None,
    dry_run: bool = True,
):
    """Admin: bulk delete agents by name prefix, owner, or explicit ID list
    (requires X-Internal-Token).

    Use dry_run=true (default) to preview which agents would be deleted.
    Set dry_run=false to actually delete.

    Filter parameters (at least one required when dry_run=false):
      - ``name_prefix``: delete all agents whose name starts with this prefix.
      - ``owner``: delete all agents belonging to this owner.
      - ``agent_ids``: comma-separated list of exact agent IDs to delete
        (e.g. ``agent_ids=acn_abc123,acn_def456``). Takes precedence over
        ``name_prefix`` / ``owner`` when all three are supplied together.

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
      ``dry_run=False`` requires at least one of ``name_prefix`` / ``owner`` /
      ``agent_ids``. Without a filter the loop would target every registered
      agent — the INTERNAL_API_TOKEN gate is the only thing standing between
      an operator typo and a full-table wipe. The guard is intentionally NOT
      applied to ``dry_run=True`` so operators can preview the full population
      before choosing a filter.
    """
    parsed_agent_ids: list[str] | None = None
    if agent_ids is not None:
        parsed_agent_ids = [aid.strip() for aid in agent_ids.split(",") if aid.strip()]
        if not parsed_agent_ids:
            raise ACNHTTPError(
                ErrorCode.INVALID_REQUEST,
                400,
                message="agent_ids must be a non-empty comma-separated list of agent IDs.",
                details={"reason": "bulk_delete_filter_required"},
            )

    if not dry_run and not name_prefix and not owner and parsed_agent_ids is None:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            message=(
                "Refusing to bulk-delete without a filter. "
                "Pass name_prefix, owner, or agent_ids explicitly. "
                "Use dry_run=true to preview filterless results."
            ),
            details={"reason": "bulk_delete_filter_required"},
        )

    # agent_ids exact-match path — skip the full population scan
    if parsed_agent_ids is not None:
        async def _fetch_by_id(aid: str):
            try:
                return await agent_service.get_agent(aid)
            except Exception:
                return None

        fetched = [(aid, await _fetch_by_id(aid)) for aid in parsed_agent_ids]
        targets = [a for _, a in fetched if a is not None]
        missing = [aid for aid, a in fetched if a is None]
        if missing:
            logger.warning("admin_bulk_delete_ids_not_found", missing=missing)
    else:
        agents = await agent_service.search_agents(tags=None, status="all")

        # Apply fuzzy filters
        targets = agents
        if name_prefix:
            targets = [a for a in targets if a.name.startswith(name_prefix)]
        if owner is not None:
            targets = [a for a in targets if (a.owner or "unowned") == owner]
        missing = []

    if dry_run:
        result: dict = {
            "dry_run": True,
            "would_delete": len(targets),
            "agents": [{"agent_id": a.agent_id, "name": a.name, "owner": a.owner} for a in targets],
        }
        if parsed_agent_ids is not None and missing:
            result["not_found"] = missing
        return result

    audit = get_audit_singleton()
    source_ip = request.client.host if request.client else None
    actor_id = request.headers.get("x-creator-id") or "admin@internal"

    # Resolve the optional FollowService so we can drop the deleted
    # agents' follow indexes alongside the agent rows. Look it up lazily
    # (rather than via Depends) because admin bulk delete predates the
    # follow subsystem and must not start 503-ing if the service was
    # not yet wired.
    from . import dependencies as _deps

    follow_svc = _deps._follow_service

    deleted, failed = [], []
    for a in targets:
        try:
            await agent_service.repository.delete(a.agent_id)
            if follow_svc is not None:
                try:
                    await follow_svc.cleanup_agent(a.agent_id)
                except Exception as cleanup_exc:  # noqa: BLE001
                    logger.warning(
                        "follow_cleanup_failed_on_bulk_delete",
                        agent_id=a.agent_id,
                        error=str(cleanup_exc),
                    )
            evict_agent_from_cache(a.agent_id)  # M3: immediate cache invalidation
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
                    "agent_ids": parsed_agent_ids,
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
    subnet_service: SubnetServiceDep = None,
    confirm: bool = Query(
        default=False,
        description=(
            "Safety guard (ACL V6 B8): must be `true` to execute this "
            "destructive operation. Omitting it or passing `false` returns "
            "a 400 so accidental calls cannot silently unregister agents."
        ),
    ),
):
    """Unregister an agent.

    **Destructive operation** — requires ``?confirm=true``.

    ADR-0006: rejected when the agent still owns one or more subnets —
    transfer or delete the subnets first (``reason="has-owned-subnets"``).

    Clean Architecture: Route → AgentService → Repository
    """
    if not confirm:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={
                "agent_id": agent_id,
                "hint": "Add ?confirm=true to confirm this destructive operation.",
            },
        )

    token_owner: str = payload.get("sub", "")

    # ADR-0006 invariant: an agent that still owns subnets cannot be deleted.
    # Transfer or delete the subnets first so the registry never holds
    # subnets with no reachable owner.
    if subnet_service is not None:
        owned = await subnet_service.list_subnets(owner=agent_id)
        if owned:
            raise ACNHTTPError(
                ErrorCode.AGENT_HAS_OWNED_SUBNETS,
                409,
                details={
                    "agent_id": agent_id,
                    "owned_subnet_ids": [s.slug for s in owned],
                    "hint": "Delete or transfer ownership of these subnets before removing the agent.",
                },
            )

    try:
        # AgentService handles authorization check
        success = await agent_service.unregister_agent(agent_id, token_owner)

        if success:
            evict_agent_from_cache(agent_id)  # M3: immediate cache invalidation
            logger.info("agent_unregistered", agent_id=agent_id, actor=token_owner)
            fire_and_forget_event(
                get_audit_singleton(),
                event_type=AuditEventType.AGENT_UNREGISTERED,
                actor_id=token_owner,
                actor_type=payload.get("type", "user"),
                target_id=agent_id,
                target_type="agent",
                details={"confirmed": True},
            )
            return {"status": "unregistered", "agent_id": agent_id}
        else:
            raise ACNHTTPError(
                ErrorCode.AGENT_NOT_FOUND,
                404,
                details={"agent_id": agent_id},
            )
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e
    except PermissionError as e:
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            403,
            details={"agent_id": agent_id, "reason": "owner_mismatch"},
        ) from e


async def _assert_no_owned_subnets(agent_id: str, subnet_service) -> None:
    """ADR-0006 guard: refuse to delete an agent that still owns subnets.

    Shared by every deletion path (owner DELETE, agent-initiated request,
    owner confirm) so the registry never ends up holding subnets with no
    reachable owner regardless of which entry point triggers the delete.
    """
    if subnet_service is None:
        return
    owned = await subnet_service.list_subnets(owner=agent_id)
    if owned:
        raise ACNHTTPError(
            ErrorCode.AGENT_HAS_OWNED_SUBNETS,
            409,
            details={
                "agent_id": agent_id,
                "owned_subnet_ids": [s.slug for s in owned],
                "hint": "Delete or transfer ownership of these subnets before removing the agent.",
            },
        )


def _emit_agent_unregistered_audit(agent_id: str, *, actor_id: str, actor_type: str) -> None:
    fire_and_forget_event(
        get_audit_singleton(),
        event_type=AuditEventType.AGENT_UNREGISTERED,
        actor_id=actor_id,
        actor_type=actor_type,
        target_id=agent_id,
        target_type="agent",
        details={"confirmed": True},
    )


class DeletionConfirmRequest(BaseModel):
    """POST body for ``/agents/{id}/deletion-request/confirm``."""

    token: str = Field(
        ...,
        max_length=128,
        description="The one-time deletion token from the deletion-request response.",
    )


@router.post("/{agent_id}/deletion-request")
@limiter.limit("10/hour")
async def request_agent_deletion(
    request: Request,
    agent_id: AgentIdPath,
    caller: OwnerOrInternalDep,
    agent_service: AgentServiceDep = None,
    subnet_service: SubnetServiceDep = None,
):
    """Agent-initiated deletion (self-service).

    Auth: ``OwnerOrInternalDep`` — the agent's own API key, or
    X-Internal-Token (ops).

    Branches on claim status:
      * **Unclaimed** (no human owner) or **internal** caller → deletes
        immediately. An unclaimed agent has no owner to confirm, and
        leaving it un-deletable was the original gap.
      * **Claimed** agent via its API key → opens a pending request that
        the human owner must confirm via
        ``POST /agents/{id}/deletion-request/confirm`` (mirrors the claim
        flow in reverse). The agent is NOT deleted yet; a
        ``pending_deletion`` marker becomes visible on the agent.

    ADR-0006: rejected (409) when the agent still owns subnets.
    """
    try:
        agent = await agent_service.get_agent(agent_id)
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND, 404, details={"agent_id": agent_id}
        ) from e

    await _assert_no_owned_subnets(agent_id, subnet_service)

    caller_kind = caller.get("caller_kind")
    is_immediate = caller_kind == "internal" or agent.owner is None

    if is_immediate:
        # Reuse the canonical delete path. owner==agent.owner passes the
        # service-layer ownership check for both unclaimed (None==None)
        # and internal-on-claimed (owner==owner) cases.
        await agent_service.unregister_agent(agent_id, agent.owner)
        evict_agent_from_cache(agent_id)
        logger.info(
            "agent_self_deleted",
            agent_id=agent_id,
            caller_kind=caller_kind,
            claim_status=agent.claim_status.value if agent.claim_status else None,
        )
        _emit_agent_unregistered_audit(
            agent_id,
            actor_id=caller.get("agent_id") or "internal",
            actor_type="agent" if caller_kind == "agent" else "system",
        )
        return {"status": "deleted", "agent_id": agent_id}

    # Claimed agent via its own API key → two-phase: open a pending request.
    _agent, token = await agent_service.request_deletion(agent_id)
    frontend_url = (settings.frontend_base_url or settings.gateway_base_url or "").rstrip("/")
    confirm_url = (
        f"{frontend_url}/agents/{agent_id}/confirm-delete?token={quote(token, safe='')}"
        if frontend_url
        else None
    )
    pending = (_agent.metadata or {}).get("deletion_request", {})
    logger.info("agent_deletion_requested", agent_id=agent_id, caller_kind=caller_kind)
    return {
        "status": "pending_confirmation",
        "agent_id": agent_id,
        "confirm_url": confirm_url,
        "expires_at": pending.get("expires_at"),
        "hint": (
            "This agent is claimed. Its human owner must confirm deletion via "
            "POST /api/v1/agents/{id}/deletion-request/confirm with the token, "
            "or open the confirm_url. The request expires at expires_at."
        ),
    }


@router.post("/{agent_id}/deletion-request/confirm")
@limiter.limit("10/hour")
async def confirm_agent_deletion(
    request: Request,
    agent_id: AgentIdPath,
    body: DeletionConfirmRequest,
    payload: dict = Depends(require_permission("acn:write")),
    agent_service: AgentServiceDep = None,
    subnet_service: SubnetServiceDep = None,
):
    """Human owner confirms an agent-initiated deletion request.

    Auth: Auth0 owner JWT with ``acn:write`` — same gate as the direct
    ``DELETE /{id}``. The caller must be the agent's owner and present the
    one-time token from the deletion-request response.

    ADR-0006: rejected (409) when the agent still owns subnets.
    """
    token_owner: str = payload.get("sub", "")
    await _assert_no_owned_subnets(agent_id, subnet_service)
    try:
        await agent_service.confirm_deletion(agent_id, token_owner, body.token)
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND, 404, details={"agent_id": agent_id}
        ) from e
    except PermissionError as e:
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            403,
            details={"agent_id": agent_id, "reason": "owner_mismatch"},
        ) from e
    except ValueError as e:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"agent_id": agent_id, "reason": "deletion_confirm_failed"},
            message=str(e),
        ) from e

    evict_agent_from_cache(agent_id)
    logger.info("agent_deletion_confirmed", agent_id=agent_id, actor=token_owner)
    _emit_agent_unregistered_audit(agent_id, actor_id=token_owner, actor_type=payload.get("type", "user"))
    return {"status": "deleted", "agent_id": agent_id}


@router.delete("/{agent_id}/deletion-request")
@limiter.limit("30/minute")
async def cancel_agent_deletion(
    request: Request,
    agent_id: AgentIdPath,
    caller: OwnerOrInternalDep,
    agent_service: AgentServiceDep = None,
):
    """Cancel a pending deletion request (agent or internal).

    Idempotent — succeeds even if there is no pending request. Clears the
    ``pending_deletion`` marker and leaves the agent fully operational.
    """
    try:
        await agent_service.cancel_deletion(agent_id)
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND, 404, details={"agent_id": agent_id}
        ) from e
    logger.info(
        "agent_deletion_cancelled",
        agent_id=agent_id,
        caller_kind=caller.get("caller_kind"),
    )
    return {"status": "cancelled", "agent_id": agent_id}


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

        # If the claim rotated the key (transfer-invite hand-off), evict the
        # old key from the auth cache immediately so the giver's previous key
        # stops authenticating within the same request, not after the TTL.
        if agent.rotated_api_key:
            evict_agent_from_cache(agent_id)

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
            api_key=agent.rotated_api_key,
        )
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e
    except ValueError as e:
        logger.warning("claim_agent_invalid_request", agent_id=agent_id, error=str(e))
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"agent_id": agent_id, "reason": "invalid_request"},
        ) from e


@router.post("/{agent_id}/transfer", response_model=AgentTransferResponse)
@limiter.limit("10/hour")
async def transfer_agent(
    request: Request,
    agent_id: AgentIdPath,
    body: AgentTransferRequest,
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
            new_owner=body.new_owner,
        )

        logger.info(
            "agent_transferred",
            agent_id=agent_id,
            from_owner=token_owner,
            to_owner=body.new_owner,
        )

        # The transfer rotated the key to lock out the previous owner; evict
        # the old key from the auth cache now. The new plaintext is NOT
        # returned to this caller (the giver) — the new owner mints a working
        # key via /rotate-key.
        if agent.rotated_api_key:
            evict_agent_from_cache(agent_id)

        return AgentTransferResponse(
            success=True,
            agent_id=agent.agent_id,
            previous_owner=token_owner,
            new_owner=agent.owner,
            message=f"Agent '{agent.name}' transferred to {body.new_owner}",
        )
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e
    except PermissionError as e:
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            403,
            details={"agent_id": agent_id, "reason": "owner_mismatch"},
        ) from e
    except ValueError as e:
        # e.g. a pending transfer invite blocks a direct transfer.
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            409,
            details={"agent_id": agent_id, "reason": str(e)},
        ) from e


@router.post("/{agent_id}/transfer-invite", response_model=AgentTransferInviteResponse)
@limiter.limit("10/hour")
async def create_transfer_invite(
    request: Request,
    agent_id: AgentIdPath,
    body: AgentTransferInviteRequest,
    payload: dict = Depends(require_permission("acn:write")),
    agent_service: AgentServiceDep = None,
):
    """Create a one-time transfer invite (P3 free gift).

    Owner remains the current user until the recipient claims with the returned token.
    Sets claim_status to ``pending_transfer``.
    """
    token_owner: str = payload.get("sub", "")

    try:
        agent = await agent_service.create_transfer_invite(
            agent_id=agent_id,
            owner=token_owner,
            ttl_seconds=body.ttl_seconds,
        )
        expires_at = agent.transfer_invite_expires_at()
        return AgentTransferInviteResponse(
            agent_id=agent.agent_id,
            verification_code=agent.verification_code or "",
            expires_at=expires_at.isoformat() if expires_at else "",
        )
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e
    except PermissionError as e:
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            403,
            details={"agent_id": agent_id, "reason": "owner_mismatch"},
        ) from e
    except ValueError as e:
        msg = str(e)
        status = 409 if "pending" in msg.lower() else 400
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            status,
            details={"agent_id": agent_id, "reason": msg},
        ) from e


@router.post("/{agent_id}/transfer-invite/cancel", response_model=AgentTransferInviteCancelResponse)
@limiter.limit("10/hour")
async def cancel_transfer_invite(
    request: Request,
    agent_id: AgentIdPath,
    payload: dict = Depends(require_permission("acn:write")),
    agent_service: AgentServiceDep = None,
):
    """Cancel a pending transfer invite and restore claimed state."""
    token_owner: str = payload.get("sub", "")

    try:
        agent = await agent_service.cancel_transfer_invite(
            agent_id=agent_id,
            owner=token_owner,
        )
        return AgentTransferInviteCancelResponse(
            success=True,
            agent_id=agent.agent_id,
            message=f"Transfer invite for '{agent.name}' cancelled",
        )
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e
    except PermissionError as e:
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            403,
            details={"agent_id": agent_id, "reason": "owner_mismatch"},
        ) from e
    except ValueError as e:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"agent_id": agent_id, "reason": str(e)},
        ) from e


@router.post("/{agent_id}/release", response_model=AgentReleaseResponse)
@limiter.limit("10/hour")
async def release_agent(
    request: Request,
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
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e
    except PermissionError as e:
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            403,
            details={"agent_id": agent_id, "reason": "owner_mismatch"},
        ) from e
    except ValueError as e:
        # e.g. a pending transfer invite blocks a direct release.
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            409,
            details={"agent_id": agent_id, "reason": str(e)},
        ) from e


@router.post("/{agent_id}/rotate-key", response_model=AgentRotateKeyResponse)
@limiter.limit("10/hour")
async def rotate_agent_api_key(
    request: Request,
    agent_id: AgentIdPath,
    background_tasks: BackgroundTasks,
    agent_service: AgentServiceDep = None,
):
    """Rotate an agent's API key (H1 — pre-launch audit).

    Returns a fresh ``acn_*`` plaintext key exactly once and immediately
    invalidates the previous one. Stored server-side as SHA-256 hash.

    Authorization (any one of):
      * The agent itself, via ``Bearer acn_<its current key>``.
      * The owner, via Auth0 JWT with ``acn:write`` permission whose
        ``sub`` matches ``agent.owner``.
      * Trusted backend, via ``X-Internal-Token`` header.

    The two-track design lets agents rotate their own credentials
    autonomously (the common case: scheduled rotation, suspected leak)
    while still letting the platform owner force-rotate a compromised
    key when the agent itself can no longer authenticate.

    Side effects:
      * Old key returns 401 on the next request (auth cache evicted).
      * No reputation / on-chain identity change — the agent_id is
        preserved, so ERC-8004 binding and subnet membership survive.
    """
    # Resolve actor via the same double-track auth we use for task writes,
    # then enforce per-agent ownership semantics below. Importing lazily
    # keeps registry.py free of a static dependency on routes/tasks.
    from .tasks import require_task_write_auth

    auth_dep = require_task_write_auth()
    payload = await auth_dep(
        request=request,
        background_tasks=background_tasks,
        credentials=await _bearer_scheme_for_rotate(request),
        x_internal_token=request.headers.get("x-internal-token"),
        agent_service=agent_service,
    )

    try:
        agent = await agent_service.get_agent(agent_id)
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e

    actor_type: str = payload.get("type", "")
    actor_sub: str = payload.get("sub", "")

    # Agent self: must be rotating ITS OWN key — anything else is a
    # privilege-escalation attempt where one agent tries to rotate
    # another agent's credential.
    if actor_type == "agent":
        if actor_sub != agent_id:
            raise ACNHTTPError(
                ErrorCode.MISSING_PERMISSION,
                403,
                details={"reason": "agent_can_only_rotate_own_key"},
            )
    # JWT path: must be the agent's owner.  Unclaimed agents (owner is
    # None) cannot be rotated via JWT — the platform never knows who
    # the rightful operator is until claim_agent runs.
    elif actor_type == "jwt":
        if not agent.owner or agent.owner != actor_sub:
            raise ACNHTTPError(
                ErrorCode.OWNERSHIP_MISMATCH,
                403,
                details={"agent_id": agent_id, "reason": "not_agent_owner"},
            )
    # ``internal``/``dev``: pass — trusted backend or dev environment.

    new_key = await agent_service.rotate_api_key(agent_id)

    # Immediately invalidate any cached auth row pointing at the old
    # key (M3). Without this the rotated key wins on lookup but the
    # OLD key can still authenticate for up to ``_API_KEY_CACHE_TTL``
    # (60s) seconds — exactly the gap a leak-and-rotate flow tries to
    # close.
    evict_agent_from_cache(agent_id)

    logger.info(
        "agent_api_key_rotated",
        agent_id=agent_id,
        actor_type=actor_type,
        actor_sub=actor_sub,
    )

    return AgentRotateKeyResponse(
        success=True,
        agent_id=agent_id,
        api_key=new_key,
        message="API key rotated. Previous key is now invalid — store the new key securely.",
    )


async def _bearer_scheme_for_rotate(request: Request):
    """Extract Bearer credentials manually for the rotate-key endpoint.

    We can't use the regular ``Depends(HTTPBearer(...))`` plumbing here
    because we're invoking ``require_task_write_auth`` programmatically
    rather than as a FastAPI dependency tree. This mirrors what the
    HTTPBearer scheme would have parsed from the Authorization header,
    returning ``None`` on absence so dev mode and internal-token paths
    still work.
    """
    from fastapi.security import HTTPAuthorizationCredentials

    auth = request.headers.get("authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=auth.split(None, 1)[1])


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
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from None

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
# L418: catch-all proxy is the most general inbound surface — every
# unrouted REST sub-path lands here. Wallet bucket is mandatory or
# the multi-agent attack just moves to ``/{id}/anything-else``.
@limiter.limit(WALLET_RATE_LIMIT, key_func=_wallet_rate_limit_key)
async def proxy_subpath(
    request: Request,
    agent_id: AgentIdPath,
    rest_path: str,
    caller: ProxyCallerDep,
    agent_service: AgentServiceDep = None,
    policy_service: PolicyServiceDep = None,
    metrics: MetricsDep = None,
):
    """Catch-all reverse proxy for agent sub-paths.

    Any request to /{agent_id}/{rest_path} that is not handled by an
    ACN-native route is transparently forwarded to the agent's real
    endpoint at {real_endpoint}/{rest_path}, preserving method, headers,
    and body.  SSE streaming is supported for text/event-stream responses.

    Requires ``X-ACN-Authorization: Bearer <ACN_API_KEY>``; rate-limit
    bucketed per calling agent.

    Subject to recipient ``communication_policy`` — a ``closed`` recipient
    returns HTTP 403 ``communication_rejected``. This is the highest-
    surface-area inbound path: anything an agent exposes via REST is
    routed through here, so policy gating must be uniform with the
    A2A and message-send paths.
    """
    return await _proxy_to_agent(
        request,
        agent_id,
        request.method,
        rest_path,
        agent_service,
        caller,
        policy_service,
        metrics,
    )
