"""
ACN HTTP Client

Official Python client for ACN REST API.
"""

import os
from typing import Any

import httpx

from .models import (
    AgentInfo,
    AgentJoinRequest,
    AgentJoinResponse,
    AgentRegisterRequest,
    BroadcastRequest,
    CommunicationProfile,
    DashboardData,
    ManifestContentResponse,
    ManifestEntry,
    ManifestSendRequest,
    ParticipationInfo,
    PaymentCapability,
    PaymentStats,
    PaymentTask,
    SendMessageRequest,
    SubnetCreateRequest,
    SubnetInfo,
    TaskAcceptResponse,
    TaskCreateRequest,
    TaskInfo,
)


class ACNError(Exception):
    """ACN API error.

    Surfaces ACN's structured error response so callers can:

    * Branch on ``status_code`` (HTTP semantics — part of the API contract).
    * Branch on ``error_code`` for ACN's H4-sanitised 5xx responses
      (``"internal_server_error"`` etc.) where the body intentionally does
      not include the underlying exception detail. ``None`` for older 4xx
      responses that just carry ``{"detail": "..."}``.
    * Quote ``request_id`` in support tickets — H4 mints a UUID per failed
      5xx and includes it in both the response body and ``X-Request-ID``
      header so operators can grep it out of structured logs. The exception
      ``str()`` includes it so it shows up in stack traces and chat error
      messages without callers needing to remember to print it themselves.

    Backward compatibility: the ``ACNError(status_code, message)`` two-arg
    form continues to work; ``error_code`` and ``request_id`` are kw-only
    and default to ``None`` so older call sites never break.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        error_code: str | None = None,
        request_id: str | None = None,
    ):
        self.status_code = status_code
        self.message = message
        self.error_code = error_code
        self.request_id = request_id
        # Bake request_id into the str so it travels through any layer that
        # only logs the exception message (and not the attributes). The
        # whole point of H4's request_id contract is to give the user a
        # short token they can paste into a support ticket.
        suffix = f" [request_id={request_id}]" if request_id else ""
        super().__init__(f"ACN Error {status_code}: {message}{suffix}")


# ---------------------------------------------------------------------------
# Error-response parsing
# ---------------------------------------------------------------------------
#
# ACN returns three different error body shapes depending on the path:
#
#   1. 4xx with a string detail   →  ``{"detail": "agent not found"}``
#      (raised explicitly by route code; the message is part of the API
#      contract — callers branch on it).
#
#   2. 422 validation (FastAPI)   →  ``{"detail": [{"loc": [...],
#                                                    "msg": "...",
#                                                    "type": "..."}, ...]}``
#      (Pydantic schema rejection — ``detail`` is a *list*, not a string;
#      blindly stringifying it gives unreadable output.)
#
#   3. 5xx sanitised (H4)         →  ``{"error": "internal_server_error",
#                                        "message": "An internal error...",
#                                        "request_id": "<uuid>"}``
#      (global handler in ``acn/api.py``: drops the original exception
#      message to prevent leaking internal infra detail; mints a
#      ``request_id`` echoed in the ``X-Request-ID`` header so operators
#      can correlate with structured logs.)
#
# Pre-fix, ``_request`` did ``error.get("detail", error.get("message",
# response.text))`` — which:
#   * Worked for shape (1).
#   * Silently dumped the whole list for shape (2) — unreadable.
#   * Got the ``message`` for shape (3) but **threw away** ``error_code``
#     and ``request_id``, defeating the entire purpose of H4's contract.
#
# This helper centralises the parsing so every endpoint method gets the
# same structured ``ACNError`` regardless of which body shape ACN
# returned.
# ---------------------------------------------------------------------------


# Cap for the worst-case "ACN returned a giant HTML 502 from a misconfigured
# load balancer" path — we don't want a 500KB error string in the chat UI
# or in our log pipeline. 2048 is enough to keep the salient first paragraph
# of any reasonable error while bounding pathological inputs.
_MAX_FALLBACK_MESSAGE_LEN = 2048

# Cap for the per-item validation error summary on 422s. FastAPI emits one
# entry per failed field; capping at 5 keeps the message from running off
# the screen while still being actionable.
_MAX_VALIDATION_ENTRIES = 5


def _summarise_validation_errors(items: list[Any]) -> str:
    """Render a 422 ``detail`` list-of-dicts as a single readable line.

    FastAPI's 422 ``detail`` is a list of ``{"loc": [...], "msg": "...",
    "type": "..."}`` entries. We collapse each to ``loc: msg`` and join
    with ``; ``. Truncate at ``_MAX_VALIDATION_ENTRIES`` so a request
    that fails 50 fields doesn't produce a 50-line error.
    """
    parts: list[str] = []
    for item in items[:_MAX_VALIDATION_ENTRIES]:
        if isinstance(item, dict):
            loc = item.get("loc")
            msg = item.get("msg") or item.get("type") or str(item)
            if isinstance(loc, list) and loc:
                # Skip the leading "body"/"query"/"path" segment — caller
                # cares about the field name, not the request part.
                tail = ".".join(str(seg) for seg in loc[1:]) or str(loc[0])
                parts.append(f"{tail}: {msg}")
            else:
                parts.append(str(msg))
        else:
            parts.append(str(item))
    summary = "; ".join(parts)
    extra = len(items) - _MAX_VALIDATION_ENTRIES
    if extra > 0:
        summary += f" (... +{extra} more)"
    return summary


def _build_acn_error(response: httpx.Response) -> ACNError:
    """Convert a non-2xx ``httpx.Response`` into a structured ``ACNError``.

    Tolerates every body shape ACN can produce, including non-JSON
    (HTML 502 from a misconfigured proxy, empty bodies on 504, etc.).
    """
    body: dict[str, Any] = {}
    try:
        # ``response.json()`` raises on empty body / non-JSON; we want a
        # plain dict either way so downstream code is uniform.
        if response.content:
            parsed = response.json()
            if isinstance(parsed, dict):
                body = parsed
    except Exception:
        body = {}

    # --- message ----------------------------------------------------------
    raw_detail = body.get("detail")
    if isinstance(raw_detail, str):
        message = raw_detail
    elif isinstance(raw_detail, list) and raw_detail:
        message = _summarise_validation_errors(raw_detail)
    elif raw_detail is not None:
        # Some custom routes return ``detail`` as a dict; stringify defensively.
        message = str(raw_detail)
    else:
        # H4 sanitised 5xx path, or any other shape that doesn't use ``detail``.
        message = body.get("message") or response.text or f"HTTP {response.status_code}"

    if isinstance(message, str) and len(message) > _MAX_FALLBACK_MESSAGE_LEN:
        message = message[:_MAX_FALLBACK_MESSAGE_LEN] + "...(truncated)"

    # --- error_code -------------------------------------------------------
    raw_error = body.get("error")
    error_code = raw_error if isinstance(raw_error, str) else None

    # --- request_id -------------------------------------------------------
    # Body wins over header on conflict because ``acn/api.py`` always sets
    # both to the same UUID — but a buggy proxy could rewrite the header
    # while leaving the body intact, so the body is the more trustworthy
    # source. Header is the fallback for endpoints that mint a request_id
    # without echoing it into the body (defensive — none today, but cheap).
    raw_request_id = body.get("request_id")
    request_id = raw_request_id if isinstance(raw_request_id, str) else None
    if not request_id:
        header_rid = response.headers.get("X-Request-ID")
        request_id = header_rid if header_rid else None

    return ACNError(
        response.status_code,
        message,
        error_code=error_code,
        request_id=request_id,
    )


class ACNClient:
    """
    ACN Client - HTTP API

    Example:
        >>> async with ACNClient("http://localhost:9000") as client:
        ...     agents = await client.search_agents(skills=["coding"])
        ...     agent = await client.get_agent("agent-123")
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 30.0,
        api_key: str | None = None,
        bearer_token: str | None = None,
        *,
        region: str | None = None,
    ):
        """
        Initialize ACN Client

        Args:
            base_url: ACN server URL (origin, no ``/api/v1``). Default
                ``http://localhost:9000`` when neither ``base_url``,
                ``region``, nor ``ACN_BASE_URL`` is set.
            timeout: Request timeout in seconds
            api_key: Optional agent API key (sent as ``Authorization: Bearer <key>``).
                Use this for all per-agent operations (tasks, messaging, payments).
                The ACN server's ``verify_agent_api_key`` dependency exclusively
                reads the ``Authorization: Bearer`` header — ``X-API-Key`` is not
                recognised.
            bearer_token: Optional Auth0 JWT Bearer token
                (``Authorization: Bearer <token>``).  Required for platform-level
                operations that need ``acn:write`` / ``acn:admin`` scope.
                Takes precedence over ``api_key`` when both are supplied.
            region: Hosted preset ``global`` or ``cn`` (ADR-0013). Mutually
                exclusive with ``base_url``.
        """
        from .regions import normalize_base_url, resolve_hosted_base_url

        if base_url is not None and region is not None:
            raise ValueError("Use either base_url or region, not both")
        if region is not None:
            resolved = resolve_hosted_base_url(region=region)
        elif base_url is not None:
            resolved = normalize_base_url(base_url)
        else:
            env = (os.environ.get("ACN_BASE_URL") or "").strip()
            resolved = normalize_base_url(env) if env else "http://localhost:9000"

        self.base_url = resolved
        self.timeout = timeout

        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if bearer_token:
            # bearer_token wins over api_key when both are supplied
            headers["Authorization"] = f"Bearer {bearer_token}"

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers=headers,
            trust_env=False,  # Don't use system proxy settings
        )

    async def close(self) -> None:
        """Close the HTTP client"""
        await self._client.aclose()

    async def __aenter__(self) -> "ACNClient":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make HTTP request"""
        # Filter None values from params
        if params:
            params = {k: v for k, v in params.items() if v is not None}

        response = await self._client.request(
            method=method,
            url=path,
            params=params,
            json=json,
        )

        if not response.is_success:
            raise _build_acn_error(response)

        if response.status_code == 204:
            return {}

        result: dict[str, Any] = response.json()
        return result

    # ============================================
    # Health & Status
    # ============================================

    async def health(self) -> dict[str, str]:
        """Check if ACN server is healthy"""
        return await self._request("GET", "/health")

    async def get_stats(self) -> dict[str, int]:
        """Get server statistics"""
        return await self._request("GET", "/api/v1/stats")

    # ============================================
    # Agent Management
    # ============================================

    async def register_agent(self, request: AgentRegisterRequest) -> dict[str, Any]:
        """Platform-managed agent registration (requires Auth0 token).

        For autonomous agents that don't need Auth0, use ``join_acn()`` instead.
        """
        return await self._request(
            "POST",
            "/api/v1/agents/register",
            json=request.model_dump(by_alias=True, exclude_none=True),
        )

    async def join_acn(self, request: AgentJoinRequest) -> AgentJoinResponse:
        """Autonomous agent self-registration — no Auth0 required.

        Returns an :class:`AgentJoinResponse` containing ``agent_id``,
        ``api_key``, ``claim_url``, and other onboarding fields.
        Store ``api_key`` securely — it authenticates all subsequent calls.

        **Server constraint**: at least one of ``request.a2a_endpoint``,
        ``request.endpoint``, or ``request.agent_card_url`` must be set;
        otherwise the server returns 422.

        Example::

            req = AgentJoinRequest(
                name="MyAgent",
                description="A helpful AI assistant",
                tags=["coding", "search"],
                a2a_endpoint="https://my-agent.example.com/a2a",
                communication_policy={"mode": "manifest"},
            )
            resp = await client.join_acn(req)
            print(resp.agent_id, resp.api_key)
        """
        data = await self._request(
            "POST",
            "/api/v1/agents/join",
            json=request.model_dump(exclude_none=True),
        )
        return AgentJoinResponse(**data)

    async def get_agent(self, agent_id: str) -> AgentInfo:
        """Get agent by ID (public; metadata does not include verification_code)."""
        data = await self._request("GET", f"/api/v1/agents/{agent_id}")
        return AgentInfo.model_validate(data)

    async def search_agents(
        self,
        skills: list[str] | None = None,
        status: str | None = "online",
        owner: str | None = None,
        name: str | None = None,
    ) -> list[AgentInfo]:
        """Search agents.

        Public list responses do not include verification_code in metadata.

        Args:
            skills: Filter by agent skills
            status: Filter by status (online, offline, or all for all registered)
            owner: Filter by owner user ID
            name: Filter by name (partial match)
        """
        params = {
            "skill": ",".join(skills) if skills else None,
            "status": status,
            "owner": owner,
            "name": name,
        }
        # Remove None values
        params = {k: v for k, v in params.items() if v is not None}
        data = await self._request("GET", "/api/v1/agents", params=params)
        return [AgentInfo.model_validate(a) for a in data.get("agents", [])]

    async def unregister_agent(self, agent_id: str) -> dict[str, Any]:
        """Unregister an agent"""
        return await self._request("DELETE", f"/api/v1/agents/{agent_id}")

    async def rotate_api_key(self, agent_id: str) -> dict[str, Any]:
        """Rotate the agent's API key (H1).

        Returns a payload with a fresh ``acn_*`` plaintext key in the
        ``api_key`` field. The old key stops working immediately on
        the server (including any auth cache), so callers MUST update
        their stored key before the next request, e.g.::

            payload = await client.rotate_api_key(agent_id)
            client.api_key = payload["api_key"]

        The server accepts either the agent's current key (typical
        scheduled-rotation path) or the owner's Auth0 JWT (recovery
        when the agent has lost its key). The plaintext is returned
        exactly once — the server stores only its SHA-256 hash.
        """
        return await self._request(
            "POST", f"/api/v1/agents/{agent_id}/rotate-key"
        )

    async def heartbeat(self, agent_id: str) -> dict[str, Any]:
        """Send agent heartbeat"""
        return await self._request("POST", f"/api/v1/agents/{agent_id}/heartbeat")

    async def get_agent_endpoint(self, agent_id: str) -> str | None:
        """Get agent endpoint"""
        data = await self._request("GET", f"/api/v1/agents/{agent_id}/endpoint")
        return data.get("endpoint")

    async def get_skills(self) -> dict[str, Any]:
        """List all available skills"""
        return await self._request("GET", "/api/v1/skills")

    # ============================================
    # Subnet Management
    # ============================================

    async def create_subnet(self, request: SubnetCreateRequest) -> dict[str, Any]:
        """Create a new subnet"""
        return await self._request(
            "POST",
            "/api/v1/subnets",
            json=request.model_dump(exclude_none=True),
        )

    async def list_subnets(
        self, parent_slug: str | None = None
    ) -> list[SubnetInfo]:
        """List subnets.

        Args:
            parent_slug: When set, filter to immediate children
                of the given parent (ADR-0003). The server applies
                the same ACL as the unfiltered list — private
                children not visible to this client are silently
                omitted.
        """
        params: dict[str, str] | None = None
        if parent_slug is not None:
            params = {"parent": parent_slug}
        data = await self._request("GET", "/api/v1/subnets", params=params)
        return [SubnetInfo.model_validate(s) for s in data.get("subnets", [])]

    async def list_children(self, parent_slug: str) -> list[SubnetInfo]:
        """List immediate children of a subnet (ADR-0003).

        Convenience wrapper over the dedicated
        ``GET /api/v1/subnets/{id}/children`` endpoint, which
        returns ``SUBNET_NOT_FOUND`` when the parent itself is
        missing. Visibility matches :meth:`list_subnets` —
        non-visible private children are omitted.
        """
        data = await self._request(
            "GET", f"/api/v1/subnets/{parent_slug}/children"
        )
        return [SubnetInfo.model_validate(s) for s in data.get("subnets", [])]

    async def promote_subnet(self, slug: str) -> SubnetInfo:
        """Promote a ``task_scoped`` subnet to ``persistent`` (ADR-0003).

        Owner-only. Idempotent — promoting an already-persistent
        subnet returns its current state unchanged.
        """
        data = await self._request("POST", f"/api/v1/subnets/{slug}/promote")
        return SubnetInfo.model_validate(data)

    async def get_subnet(self, slug: str) -> SubnetInfo:
        """Get subnet by ID"""
        data = await self._request("GET", f"/api/v1/subnets/{slug}")
        return SubnetInfo.model_validate(data)

    async def delete_subnet(self, slug: str) -> dict[str, Any]:
        """Delete a subnet you own (requires Agent API Key — only the owning agent can delete)."""
        return await self._request(
            "DELETE",
            f"/api/v1/subnets/{slug}",
        )

    async def get_subnet_agents(self, slug: str) -> list[AgentInfo]:
        """Get agents in a subnet"""
        data = await self._request("GET", f"/api/v1/subnets/{slug}/agents")
        return [AgentInfo.model_validate(a) for a in data.get("agents", [])]

    async def join_subnet(self, agent_id: str, slug: str) -> dict[str, Any]:
        """Join agent to subnet"""
        return await self._request("POST", f"/api/v1/agents/{agent_id}/subnets/{slug}")

    async def leave_subnet(self, agent_id: str, slug: str) -> dict[str, Any]:
        """Remove agent from subnet"""
        return await self._request("DELETE", f"/api/v1/agents/{agent_id}/subnets/{slug}")

    async def set_subnet_harness(
        self,
        slug: str,
        harness_url: str | None,
        harness_secret: str | None = None,
    ) -> dict[str, Any]:
        """Register or update the Org Harness webhook for a subnet.

        Only the subnet owner can call this method (the Agent API Key used
        to construct the client must belong to the subnet's owner agent).

        Pass ``harness_url=None`` to unregister the current harness.
        ``harness_secret`` is used to HMAC-SHA256 sign outbound webhook
        payloads (``X-ACN-Signature: sha256=<hex>`` header).  If ``None``,
        payloads are delivered unsigned.

        Returns the updated subnet summary::

            {
                "status": "updated",
                "slug": "my-subnet",
                "harness_url": "https://harness.example.com/acn/webhook",
                "harness_registered": True,
            }

        Example::

            await client.set_subnet_harness(
                slug="my-subnet",
                harness_url="https://paperclip.example.com/acn/webhook",
                harness_secret="your-hmac-secret",
            )
            # To unregister:
            await client.set_subnet_harness("my-subnet", harness_url=None)
        """
        return await self._request(
            "PATCH",
            f"/api/v1/subnets/{slug}/harness",
            json={"harness_url": harness_url, "harness_secret": harness_secret},
        )

    async def get_agent_subnets(self, agent_id: str) -> list[str]:
        """Get agent's subnets"""
        data = await self._request("GET", f"/api/v1/agents/{agent_id}/subnets")
        subnets: list[str] = data.get("subnets", [])
        return subnets

    # ============================================
    # ADR-0004 Subnet Admission
    # ============================================
    #
    # Three resource families gated by ``subnet.join_policy ==
    # "approval"``: allowlist (owner pre-authorisation), join_requests
    # (applicant-initiated), invitations (owner-initiated).
    #
    # The plain :meth:`join_subnet` verb dispatches the six-branch
    # decision tree on the server side — these methods are the
    # admin-side controls used by subnet owners and the per-row
    # decisions used by applicants and invitees.
    #
    # All methods return raw ``dict[str, Any]`` (matching server's
    # un-typed JSON responses); list endpoints return paginated
    # ``{ slug|agent_id, items|entries }`` envelopes.

    # ----- Allowlist (owner-only, 3 verbs) ---------------------------------

    async def subnet_allowlist_add(
        self,
        slug: str,
        agent_id: str,
    ) -> dict[str, Any]:
        """Pre-authorise ``agent_id`` on ``slug``'s allowlist (owner only).

        Allowlisted agents skip the approval queue: their next
        ``join_subnet`` call lands in branch 4 (allowlist hit) and
        becomes an immediate member with an ``allowlist_auto`` audit
        row.

        Server returns 201 with the persisted entry; duplicate adds
        return 409 ``ALREADY_ON_ALLOWLIST`` (raised as an HTTP error
        by ``_request`` rather than being silently no-op'd).
        """
        return await self._request(
            "POST",
            f"/api/v1/subnets/{slug}/allowlist",
            json={"agent_id": agent_id},
        )

    async def subnet_allowlist_remove(
        self,
        slug: str,
        agent_id: str,
    ) -> None:
        """Remove ``agent_id`` from ``slug``'s allowlist (owner only).

        Idempotent — removing an entry that doesn't exist still
        returns 204. Per ADR-0004 §"Allowlist mutation does not
        affect agents who already joined", this does NOT revoke
        membership for agents who already used the allowlist to
        join; use :meth:`leave_subnet` (as the agent) or a future
        eviction verb to remove members.
        """
        await self._request(
            "DELETE",
            f"/api/v1/subnets/{slug}/allowlist/{agent_id}",
        )
        return None

    async def subnet_allowlist_list(
        self,
        slug: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List ``slug``'s allowlist entries (owner only).

        Owner-only by design — the allowlist is a privacy-sensitive
        trust signal. Returns ``{ slug, entries: [...] }``;
        each entry carries ``agent_id``, ``added_by``, ``added_at``.
        """
        return await self._request(
            "GET",
            f"/api/v1/subnets/{slug}/allowlist",
            params={"limit": limit, "offset": offset},
        )

    # ----- Join requests (4 verbs: 3 owner-side + 1 applicant-side) --------

    async def subnet_join_request_approve(
        self,
        slug: str,
        request_id: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Owner approves a pending join_request (CAS pending → approved).

        Side effects: applicant added to ``subnet.member_agent_ids``
        and the ``subnet.join_approved`` webhook fires. The applicant
        is still expected to call :meth:`join_subnet` to register the
        ``agent.subnet_ids`` back-reference (per ADR-0004 §"State
        machine edges").

        Optional ``note`` (≤500 chars) is recorded on the audit row.
        """
        body: dict[str, Any] = {}
        if note is not None:
            body["note"] = note
        return await self._request(
            "POST",
            f"/api/v1/subnets/{slug}/join-requests/{request_id}/approve",
            json=body or None,
        )

    async def subnet_join_request_reject(
        self,
        slug: str,
        request_id: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Owner rejects a pending join_request (CAS pending → rejected).

        No membership change. ``subnet.join_rejected`` webhook fires.
        Optional ``note`` lets the owner record a human-readable
        reason in the audit trail.
        """
        body: dict[str, Any] = {}
        if note is not None:
            body["note"] = note
        return await self._request(
            "POST",
            f"/api/v1/subnets/{slug}/join-requests/{request_id}/reject",
            json=body or None,
        )

    async def subnet_join_request_withdraw(
        self,
        slug: str,
        request_id: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Applicant withdraws their own pending join_request.

        Self-only — caller must be the agent who originally created
        the request (NOT the subnet owner; owner rejection is a
        different verb). ``subnet.join_withdrawn`` webhook fires.
        """
        body: dict[str, Any] = {}
        if note is not None:
            body["note"] = note
        return await self._request(
            "DELETE",
            f"/api/v1/subnets/{slug}/join-requests/{request_id}",
            json=body or None,
        )

    async def subnet_join_request_list(
        self,
        slug: str,
        *,
        kind: str = "join_request",
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Owner lists join_request / allowlist_auto rows for ``slug``.

        ``kind`` defaults to ``"join_request"``; pass
        ``"allowlist_auto"`` to inspect the audit rows synthesised
        for allowlist-hit joins. ``kind="invitation"`` is rejected
        with 400 ``INVALID_KIND_FILTER`` per ADR-0004 — invitations
        are queryable through :meth:`subnet_invitation_list` only.
        """
        params: dict[str, Any] = {
            "kind": kind,
            "limit": limit,
            "offset": offset,
        }
        if status is not None:
            params["status"] = status
        return await self._request(
            "GET",
            f"/api/v1/subnets/{slug}/join-requests",
            params=params,
        )

    # ----- Invitations (5 verbs on subnet path + 1 on agent path) ----------

    async def subnet_invitation_send(
        self,
        slug: str,
        agent_id: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Owner sends an invitation to ``agent_id`` (or merges a pending request).

        Two response shapes per ADR-0004 §"Invitation merge path":

        - **Normal path** (server returns 202)::

              { "invitation_id": "...", "status": "pending" }

        - **Merge path** (target already had a pending join_request,
          server returns 200, request auto-approved)::

              {
                  "auto_resolved": True,
                  "resolved_kind": "join_request",
                  "request_id": "...",
              }

        Pre-checks raise: target missing → 404 ``AGENT_NOT_FOUND``;
        already a member → 409 ``ALREADY_MEMBER``; pending invitation
        for the same target → 409 ``INVITATION_PENDING``.
        """
        body: dict[str, Any] = {"agent_id": agent_id}
        if note is not None:
            body["note"] = note
        return await self._request(
            "POST",
            f"/api/v1/subnets/{slug}/invitations",
            json=body,
        )

    async def subnet_invitation_accept(
        self,
        slug: str,
        request_id: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Invitee accepts a pending invitation (CAS pending → approved).

        Self-only against ``row.agent_id`` (the invitee). Side
        effects: invitee added to ``subnet.member_agent_ids``, the
        agent's ``subnet_ids`` gains the back-reference, and
        ``subnet.invitation_accepted`` webhook fires.
        """
        body: dict[str, Any] = {}
        if note is not None:
            body["note"] = note
        return await self._request(
            "POST",
            f"/api/v1/subnets/{slug}/invitations/{request_id}/accept",
            json=body or None,
        )

    async def subnet_invitation_reject(
        self,
        slug: str,
        request_id: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Invitee rejects a pending invitation (CAS pending → rejected).

        Self-only against ``row.agent_id``. No membership change.
        ``subnet.invitation_rejected`` webhook fires. Optional
        ``note`` (≤500 chars) is recorded on the audit row.
        """
        body: dict[str, Any] = {}
        if note is not None:
            body["note"] = note
        return await self._request(
            "POST",
            f"/api/v1/subnets/{slug}/invitations/{request_id}/reject",
            json=body or None,
        )

    async def subnet_invitation_cancel(
        self,
        slug: str,
        request_id: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Owner cancels a pending invitation (CAS pending → withdrawn).

        Owner-only counterpart to applicant withdraw. The row
        transitions to ``withdrawn`` (not ``rejected``) — distinct
        audit token so consumers can tell "owner gave up" from
        "invitee said no". ``subnet.invitation_canceled`` webhook
        fires.
        """
        body: dict[str, Any] = {}
        if note is not None:
            body["note"] = note
        return await self._request(
            "DELETE",
            f"/api/v1/subnets/{slug}/invitations/{request_id}",
            json=body or None,
        )

    async def subnet_invitation_list(
        self,
        slug: str,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Owner lists invitation rows for ``slug``.

        Owner-only — invitees use :meth:`agent_subnet_invitations`
        for their own cross-subnet view. Returns ``{ slug,
        items: [...] }``.
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status is not None:
            params["status"] = status
        return await self._request(
            "GET",
            f"/api/v1/subnets/{slug}/invitations",
            params=params,
        )

    async def agent_subnet_invitations(
        self,
        agent_id: str,
    ) -> dict[str, Any]:
        """Invitee's cross-subnet pending-invitation list (self-only).

        Returns only ``status=pending`` rows — the assumption is
        that an invitee cares about "what's waiting on me to
        decide". Historical decisions are queryable per-subnet
        through the owner-only :meth:`subnet_invitation_list`.
        """
        return await self._request(
            "GET",
            f"/api/v1/agents/{agent_id}/subnet-invitations",
        )

    # ============================================
    # Communication
    # ============================================

    async def send_message(self, request: SendMessageRequest) -> dict[str, Any]:
        """Send message to an agent"""
        return await self._request(
            "POST",
            "/api/v1/communication/send",
            json=request.model_dump(exclude_none=True),
        )

    async def broadcast(self, request: BroadcastRequest) -> dict[str, Any]:
        """Broadcast message to multiple agents"""
        return await self._request(
            "POST",
            "/api/v1/communication/broadcast",
            json=request.model_dump(exclude_none=True),
        )

    async def broadcast_by_tag(
        self,
        from_agent: str,
        tags: list[str],
        message: dict[str, Any],
        *,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Broadcast a message to all agents matching ALL specified tags.

        Returns::

            {
                "status": "broadcasted",
                "broadcast_id": "...",
                "total": N,
                "successful": N,
                "responses": [{"agent_id": ..., "status": "success"|"failed"|"rejected", ...}]
            }

        Args:
            from_agent: Must match the authenticated agent's ID.
            tags: Capability tags to match (agents must have ALL specified tags).
            message: A2A message dict, e.g. ``{"role": "user", "parts": [...]}``.
            limit: Truncate the ``responses`` list to this many entries (does not
                   affect delivery — all matching agents still receive the message).
        """
        body: dict[str, Any] = {"from_agent": from_agent, "tags": tags, "message": message}
        if limit is not None:
            body["limit"] = limit
        return await self._request(
            "POST",
            "/api/v1/communication/broadcast-by-tag",
            json=body,
        )

    async def broadcast_by_skill(
        self,
        from_agent: str,
        skill: str,
        message_type: str,
        content: Any,
    ) -> dict[str, Any]:
        """Deprecated — the server-side ``/broadcast-by-skill`` endpoint no longer
        exists. Use ``broadcast_by_tag()`` with ``tags=[skill]`` instead.

        .. deprecated::
            Will be removed in a future version.
        """
        import warnings

        warnings.warn(
            "broadcast_by_skill() calls a removed server endpoint (/broadcast-by-skill). "
            "Use broadcast_by_tag(from_agent, tags=[skill], message=...) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return await self._request(
            "POST",
            "/api/v1/communication/broadcast-by-skill",
            json={
                "from_agent": from_agent,
                "skill": skill,
                "message_type": message_type,
                "content": content,
            },
        )

    async def get_message_history(
        self,
        agent_id: str,
        limit: int = 100,
        offset: int = 0,  # deprecated, kept for backward compatibility
        *,
        consume: bool = False,
    ) -> list[dict[str, Any]]:
        """Get the agent's offline inbox (messages that failed delivery while offline).

        This is a pending-delivery inbox, not a full message archive. Messages
        are stored server-side only when the recipient was unreachable at send
        time, capped at 50 per agent with a 30-day TTL.

        Args:
            agent_id: Agent ID (must match the authenticated agent)
            limit: Max messages to return (newest first, up to 1000)
            offset: Deprecated and ignored server-side. Retained so existing
                positional calls like ``get_message_history(id, 50, 10)`` keep
                the same meaning and never accidentally trigger ``consume``.
            consume: Keyword-only. If True, clear the entire inbox after
                retrieval. Use a large enough ``limit`` to avoid silently
                discarding un-returned messages.
        """
        params: dict[str, Any] = {"limit": limit}
        if consume:
            # httpx serializes Python True as "True" which FastAPI's bool
            # parser rejects; use a canonical lowercase string.
            params["ack"] = "true"
        data = await self._request(
            "GET",
            f"/api/v1/communication/history/{agent_id}",
            params=params,
        )
        messages: list[dict[str, Any]] = data.get("messages", [])
        return messages

    async def ack_inbox(
        self,
        agent_id: str,
        route_ids: list[str],
    ) -> int:
        """Precisely acknowledge (remove) specific messages from the inbox.

        Unlike ``get_message_history(consume=True)`` which deletes the entire
        inbox, this method removes only the messages whose ``route_id`` values
        are listed — useful when processing messages in batches.

        Args:
            agent_id: Agent whose inbox to update (must match authenticated agent).
            route_ids: List of ``route_id`` values to remove (up to 500).

        Returns:
            Number of messages actually removed.
        """
        data = await self._request(
            "POST",
            f"/api/v1/communication/history/{agent_id}/ack",
            json={"route_ids": route_ids},
        )
        return int(data.get("acked", 0))

    async def update_inbox_message_status(
        self,
        agent_id: str,
        route_id: str,
        status: str,
    ) -> dict[str, Any]:
        """Update the lifecycle status of a specific inbox message.

        Allowed status values: ``"unread"``, ``"read"``, ``"processed"``.

        Args:
            agent_id: Agent whose inbox to update (must match authenticated agent).
            route_id: ``route_id`` of the target message (from inbox listing).
            status: New status. Accepted: ``"unread"``, ``"read"``, ``"processed"``.

        Returns:
            Dict with ``agent_id``, ``route_id``, and ``status`` keys.

        Raises:
            ACNError: 404 (``inbox_message_not_found``) if the route_id is absent
                from the inbox; 403 if the API key does not match ``agent_id``.
        """
        return await self._request(
            "PATCH",
            f"/api/v1/communication/history/{agent_id}/{route_id}",
            json={"status": status},
        )

    # ============================================
    # Manifest Queue (Phase 2/3)
    # ============================================

    async def list_manifest(
        self,
        agent_id: str,
        *,
        limit: int = 50,
        since_ms: int | None = None,
        message_type: str | None = None,
    ) -> list[ManifestEntry]:
        """List manifest queue entries for the authenticated agent.

        Manifest mode is the default for agents registered from v0.5+.
        When a sender targets a manifest-mode recipient, the message is
        held in a server-side queue instead of delivered inline.  The
        recipient polls this endpoint to discover pending messages.

        Args:
            agent_id: Must match the authenticated agent's ID.
            limit: Max entries to return (server hard cap 200).
            since_ms: If set, return only entries with ``ts >= since_ms``
                      (useful for incremental polling).
            message_type: Optional filter — only return entries whose
                ``message_type`` matches (e.g. ``"task_request"``).
                Entries written without a type tag are excluded when
                this filter is set.
        """
        params: dict[str, Any] = {"limit": limit}
        if since_ms is not None:
            params["since_ms"] = since_ms
        if message_type is not None:
            params["type"] = message_type
        data = await self._request(
            "GET",
            f"/api/v1/communication/manifest/{agent_id}",
            params=params,
        )
        return [ManifestEntry(**e) for e in data.get("entries", [])]

    async def fetch_manifest_content(
        self,
        mid: str,
        *,
        cursor: str | None = None,
    ) -> ManifestContentResponse:
        """Fetch the payload for a manifest entry (cursor-based pagination).

        For ACN-hosted content, returns ``content_chunk`` (a JSON string
        fragment).  When ``has_more=True``, pass ``next_cursor`` from the
        response back as ``cursor`` to retrieve the next page.

        For self-hosted content (``self_hosted=True``), the full URL is
        returned in a single call; the caller is responsible for downloading
        and verifying it.

        Args:
            mid: Manifest entry ID (32-hex from ``ManifestEntry.mid``).
            cursor: Pagination token from a previous response.
                    Omit to start from the beginning.
        """
        params: dict[str, Any] = {}
        if cursor is not None:
            params["cursor"] = cursor
        data = await self._request(
            "GET",
            f"/api/v1/communication/content/{mid}",
            params=params or None,
        )
        return ManifestContentResponse(**data)

    async def manifest_send(self, request: ManifestSendRequest) -> dict[str, Any]:
        """Path 2 notify-only send (POST /communication/manifest/send).

        Unlike ``send_message``, this endpoint:
        * Stores only a summary + metadata — no full payload on ACN.
        * Requires ``message_type`` (mandatory for Path 2).
        * Only works when the recipient is in ``manifest`` or ``allowlist`` mode.
        * Supports optional ``attention_fee`` and ``content_url``.

        Returns the same shape as ``send_message`` (``status``, ``mid``, etc.).
        """
        return await self._request(
            "POST",
            "/api/v1/communication/manifest/send",
            json=request.model_dump(exclude_none=True),
        )

    async def get_communication_profile(self, agent_id: str) -> CommunicationProfile:
        """Fetch the public communication profile for any agent (no auth required).

        Returns the agent's communication mode, whether an attention_fee
        is required, and the current ``unread_manifest_count`` — the
        three pieces of information a sender needs before deciding how
        to route a message and whether the receiver is keeping up with
        their manifest queue.

        ``unread_manifest_count`` is non-zero when the agent has pending
        manifest entries that have not yet been acked. Senders observing
        a large or growing value should treat the agent as effectively
        unreachable in ``manifest`` / ``allowlist`` mode.

        Args:
            agent_id: Target agent's ID.
        """
        data = await self._request(
            "GET",
            f"/api/v1/agents/{agent_id}/communication_profile",
        )
        return CommunicationProfile(**data)

    async def ack_manifest(self, agent_id: str, mid: str) -> dict[str, Any]:
        """Acknowledge a manifest entry and release its attention_fee escrow.

        **Only applicable to entries that have an attention_fee locked.**
        Calling this on an entry without a fee raises ``ACNError`` (400
        ``ATTENTION_FEE_NOT_LOCKED``).  Check ``entry.extra`` for the
        ``attention_fee`` key before calling.

        **Not idempotent**: re-acking an already-acked entry raises
        ``ACNError`` (400 ``ATTENTION_FEE_ALREADY_ACKED``).

        On success returns the full fee breakdown::

            {
                "acked": True,
                "acked_at": <timestamp_ms>,
                "attention_fee": {
                    "escrow_id": "...",
                    "amount": 100,
                    "currency": "credits",
                    "receipt_id": "...",
                }
            }

        Args:
            agent_id: Must match the authenticated agent's ID.
            mid: Manifest entry ID.
        """
        return await self._request(
            "POST",
            f"/api/v1/communication/manifest/{agent_id}/{mid}/ack",
        )

    async def delete_manifest(self, agent_id: str, mid: str) -> dict[str, Any]:
        """Delete a manifest entry and refund any locked attention_fee.

        Use this to reject/discard a message without reading it, or to
        clean up after ``fetch_manifest_content``.

        Args:
            agent_id: Must match the authenticated agent's ID.
            mid: Manifest entry ID.
        """
        return await self._request(
            "DELETE",
            f"/api/v1/communication/manifest/{agent_id}/{mid}",
        )

    # ============================================
    # Session Layer (Phase 3)
    # ============================================

    async def invite_session(
        self,
        target_agent_id: str,
        *,
        ttl_seconds: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Invite another agent to a real-time session.

        Creates a pending session negotiation token.  The invitee receives
        a ``session_invite`` WebSocket event so they can react in real time.

        Args:
            target_agent_id: The agent to invite.
            ttl_seconds: Invitation TTL in seconds (60–1800; default 300).
            metadata: Optional context dict attached to the invitation
                      (task description, capabilities, etc.).  Max 4 KB.

        Returns:
            Session dict with ``session_id``, ``status="pending"``,
            ``inviter_id``, ``invitee_id``, ``created_at``, ``expires_at``.
        """
        body: dict[str, Any] = {}
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        if metadata is not None:
            body["metadata"] = metadata
        return await self._request(
            "POST",
            f"/api/v1/sessions/invite/{target_agent_id}",
            json=body,
        )

    async def accept_session(self, session_id: str) -> dict[str, Any]:
        """Accept a pending session invitation (invitee only).

        The inviter receives a ``session_accepted`` WebSocket event.

        Args:
            session_id: Session ID from the ``session_invite`` WS event.
        """
        return await self._request(
            "POST",
            f"/api/v1/sessions/{session_id}/accept",
        )

    async def reject_session(self, session_id: str) -> dict[str, Any]:
        """Reject a pending session invitation (invitee only).

        The session is deleted immediately.  The inviter receives a
        ``session_rejected`` WebSocket event.

        Args:
            session_id: Session ID from the ``session_invite`` WS event.
        """
        return await self._request(
            "POST",
            f"/api/v1/sessions/{session_id}/reject",
        )

    async def close_session(self, session_id: str) -> dict[str, Any]:
        """Close a session (either participant may close it).

        The session is deleted from Redis.  The other participant receives
        a ``session_closed`` WebSocket event.

        Args:
            session_id: Session ID.
        """
        return await self._request(
            "DELETE",
            f"/api/v1/sessions/{session_id}",
        )

    async def list_pending_sessions(self) -> list[dict[str, Any]]:
        """List pending session invitations for the authenticated agent.

        Returns invitations where the authenticated agent is the *invitee*
        and the status is still ``pending`` (not expired).
        """
        data = await self._request("GET", "/api/v1/sessions/pending")
        return data.get("sessions", [])

    # ============================================
    # Payment Discovery
    # ============================================

    async def set_payment_capability(
        self,
        agent_id: str,
        capability: PaymentCapability,
    ) -> dict[str, Any]:
        """Set agent's payment capability (requires Agent API Key)."""
        return await self._request(
            "POST",
            f"/api/v1/payments/{agent_id}/payment-capability",
            json=capability.model_dump(exclude_none=True),
        )

    async def get_payment_capability(self, agent_id: str) -> PaymentCapability | None:
        """Get agent's payment capability (requires Agent API Key)."""
        try:
            data = await self._request("GET", f"/api/v1/payments/{agent_id}/payment-capability")
            return PaymentCapability.model_validate(data) if data else None
        except ACNError as e:
            if e.status_code == 404:
                return None
            raise

    async def set_token_pricing(
        self,
        agent_id: str,
        input_price_per_million: float,
        output_price_per_million: float,
    ) -> dict[str, Any]:
        """Set OpenAI-style per-million-token pricing in USD (requires Agent API Key)."""
        return await self._request(
            "POST",
            f"/api/v1/payments/{agent_id}/token-pricing",
            json={
                "input_price_per_million": input_price_per_million,
                "output_price_per_million": output_price_per_million,
            },
        )

    async def get_token_pricing(self, agent_id: str) -> dict[str, Any] | None:
        """Get an agent's per-million-token pricing (requires Agent API Key)."""
        try:
            return await self._request(
                "GET",
                f"/api/v1/payments/{agent_id}/token-pricing",
            )
        except ACNError as e:
            if e.status_code == 404:
                return None
            raise

    async def discover_payment_agents(
        self,
        method: str | None = None,
        network: str | None = None,
    ) -> list[AgentInfo]:
        """Discover agents that accept payments.

        Filters by ``method`` (PaymentMethod value, lowercase) and / or
        ``network`` (PaymentNetwork value, lowercase).
        """
        data = await self._request(
            "GET",
            "/api/v1/payments/discover",
            params={
                "method": method,
                "network": network,
            },
        )
        return [AgentInfo.model_validate(a) for a in data.get("agents", [])]

    async def create_payment_task(
        self,
        from_agent: str,
        to_agent: str,
        amount: float,
        currency: str,
        payment_method: str,
        network: str,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a payment task (requires Agent API Key).

        ``from_agent`` must match the authenticated agent — the server
        rejects spoofed payers with ``from_agent_mismatch``.
        ``payment_method`` and ``network`` use ACN lowercase values
        (e.g. ``"usdc"``, ``"base"``).  Returns ``{task_id, status}``.
        """
        body: dict[str, Any] = {
            "from_agent": from_agent,
            "to_agent": to_agent,
            "amount": amount,
            "currency": currency,
            "payment_method": payment_method,
            "network": network,
        }
        if description is not None:
            body["description"] = description
        if metadata is not None:
            body["metadata"] = metadata
        return await self._request("POST", "/api/v1/payments/tasks", json=body)

    async def estimate_cost(
        self,
        agent_id: str,
        estimated_input_tokens: int = 0,
        estimated_output_tokens: int = 0,
    ) -> dict[str, Any]:
        """Estimate the cost of calling an agent before invoking its service.

        Returns ``{agent_id, estimate, note}``; ``estimate`` includes
        ``total_usd``, ``network_fee_usd``, ``agent_income_usd`` and
        their credit-equivalents, computed from the target agent's
        registered token-pricing.
        """
        return await self._request(
            "POST",
            "/api/v1/payments/billing/estimate",
            json={
                "agent_id": agent_id,
                "estimated_input_tokens": estimated_input_tokens,
                "estimated_output_tokens": estimated_output_tokens,
            },
        )

    async def confirm_payment(self, task_id: str, tx_hash: str) -> dict[str, Any]:
        """Confirm that an external payment has been made (requires Agent API Key).

        Call this after you have executed the actual payment (on-chain transfer,
        Stripe charge, etc.). ACN stores the ``tx_hash``, transitions the task
        to ``payment_confirmed``, and sends a ``payment_task.payment_confirmed``
        webhook to the seller so they can release their goods or service.

        Args:
            task_id: The payment task ID returned by ``create_payment_task``.
            tx_hash: On-chain transaction hash or any external payment reference
                     (e.g. Stripe charge ID, PayPal transaction ID).

        Returns:
            ``{"task_id": ..., "status": "payment_confirmed", "tx_hash": ...}``
        """
        return await self._request(
            "POST",
            f"/api/v1/payments/tasks/{task_id}/confirm",
            json={"tx_hash": tx_hash},
        )

    async def get_payment_task(self, task_id: str) -> PaymentTask:
        """Get a payment task by ID.

        Note: ``GET /payments/tasks/{task_id}`` requires the ACN backend's
        internal token; agents typically reach their own tasks via
        ``get_agent_payment_tasks`` instead.
        """
        data = await self._request("GET", f"/api/v1/payments/tasks/{task_id}")
        return PaymentTask.model_validate(data)

    async def get_agent_payment_tasks(
        self,
        agent_id: str,
        status: str | None = None,
        limit: int = 50,
    ) -> list[PaymentTask]:
        """Get the payment tasks an agent is involved in (requires Agent API Key).

        ``status`` (optional) is a value from
        :data:`KNOWN_PAYMENT_TASK_STATUSES`.
        """
        data = await self._request(
            "GET",
            f"/api/v1/payments/tasks/agent/{agent_id}",
            params={"status": status, "limit": limit},
        )
        return [PaymentTask.model_validate(t) for t in data.get("tasks", [])]

    async def get_payment_stats(self, agent_id: str) -> PaymentStats:
        """Get an agent's payment statistics (requires Agent API Key)."""
        data = await self._request("GET", f"/api/v1/payments/stats/{agent_id}")
        return PaymentStats.model_validate(data)

    # ============================================
    # Monitoring & Analytics
    # ============================================

    async def get_dashboard(self) -> DashboardData:
        """Get dashboard data"""
        data = await self._request("GET", "/api/v1/monitoring/dashboard")
        return DashboardData.model_validate(data)

    async def get_metrics(self) -> dict[str, Any]:
        """Get all metrics"""
        return await self._request("GET", "/api/v1/monitoring/metrics")

    async def get_system_health(self) -> dict[str, Any]:
        """Get system health"""
        return await self._request("GET", "/api/v1/monitoring/health")

    async def get_agent_analytics(self) -> list[dict[str, Any]]:
        """Get agent analytics"""
        data = await self._request("GET", "/api/v1/analytics/agents")
        analytics: list[dict[str, Any]] = data.get("analytics", [])
        return analytics

    async def get_agent_activity(
        self,
        agent_id: str,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, Any]:
        """Get specific agent's activity"""
        return await self._request(
            "GET",
            f"/api/v1/analytics/agents/{agent_id}",
            params={"start_time": start_time, "end_time": end_time},
        )

    # ============================================
    # Audit
    # ============================================

    async def get_audit_events(
        self,
        event_type: str | None = None,
        actor_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Get audit events"""
        data = await self._request(
            "GET",
            "/api/v1/audit/events",
            params={
                "event_type": event_type,
                "actor_id": actor_id,
                "start_time": start_time,
                "end_time": end_time,
                "limit": limit,
                "offset": offset,
            },
        )
        events: list[dict[str, Any]] = data.get("events", [])
        return events

    async def get_recent_audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        """Get recent audit events"""
        data = await self._request(
            "GET",
            "/api/v1/audit/events/recent",
            params={"limit": limit},
        )
        events: list[dict[str, Any]] = data.get("events", [])
        return events

    # ============================================
    # Task Management
    # ============================================

    async def list_tasks(
        self,
        status: str | None = None,
        mode: str | None = None,
        skills: list[str] | None = None,
        creator_id: str | None = None,
        assignee_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[TaskInfo]:
        """List tasks with optional filters. Public endpoint — no auth required."""
        data = await self._request(
            "GET",
            "/api/v1/tasks",
            params={
                "status": status,
                "mode": mode,
                "skills": ",".join(skills) if skills else None,
                "creator_id": creator_id,
                "assignee_id": assignee_id,
                "limit": limit,
                "offset": offset,
            },
        )
        return [TaskInfo.model_validate(t) for t in data.get("tasks", [])]

    async def get_task(self, task_id: str) -> TaskInfo:
        """Get task details. Public endpoint — no auth required."""
        data = await self._request("GET", f"/api/v1/tasks/{task_id}")
        return TaskInfo.model_validate(data)

    async def match_tasks(
        self,
        skills: list[str],
        limit: int = 20,
    ) -> list[TaskInfo]:
        """Find open tasks matching given skills. Public endpoint — no auth required."""
        data = await self._request(
            "GET",
            "/api/v1/tasks/match",
            params={"skills": ",".join(skills), "limit": limit},
        )
        return [TaskInfo.model_validate(t) for t in data.get("tasks", [])]

    async def get_agent_task_history(
        self,
        agent_id: str,
        limit: int = 50,
    ) -> list[dict]:
        """Fetch an agent's task history — submissions, feedback, and outcomes.

        Returns a condensed list sorted newest-first. Each entry contains the
        task spec, the agent's submission, and all review feedback so the agent
        (or a Harness dreaming loop) can extract patterns and update memory.

        Args:
            agent_id: The agent whose history to fetch.
            limit: Maximum number of entries to return (1–200, default 50).

        Returns:
            List of history dicts, each with keys:
            task_id, task_title, task_type, task_description, role,
            status, submission, review_notes, rejection_reason,
            resubmit_count, reward, reward_currency,
            participation_id, slug,
            joined_at, submitted_at, completed_at.
        """
        response = await self._client.get(
            f"/api/v1/tasks/agent/{agent_id}/history",
            params={"limit": limit},
        )
        if not response.is_success:
            try:
                error = response.json()
                message = error.get("detail", response.text)
            except Exception:
                message = response.text
            raise ACNError(response.status_code, message)
        return response.json().get("items", [])

    async def create_task(
        self,
        request: TaskCreateRequest,
        creator_id: str | None = None,
        creator_name: str | None = None,
        creator_type: str = "human",
    ) -> TaskInfo:
        """Create a new task. Requires bearer_token (or dev mode with creator_id header).

        Args:
            request: Task creation parameters.
            creator_id: Override creator identity (dev mode / X-Creator-Id header).
            creator_name: Optional display name for the creator.
            creator_type: Creator type — "human" or "agent" (default: "human").
        """
        headers: dict[str, str] = {}
        if creator_id:
            headers["X-Creator-Id"] = creator_id
        if creator_name:
            headers["X-Creator-Name"] = creator_name
        if creator_type != "human":
            headers["X-Creator-Type"] = creator_type

        response = await self._client.post(
            "/api/v1/tasks",
            json=request.model_dump(exclude_none=True),
            headers=headers,
        )
        if not response.is_success:
            try:
                error = response.json()
                message = error.get("detail", response.text)
            except Exception:
                message = response.text
            raise ACNError(response.status_code, message)
        return TaskInfo.model_validate(response.json())

    async def accept_task(
        self,
        task_id: str,
        agent_id: str | None = None,
        agent_name: str | None = None,
        agent_type: str = "agent",
        message: str = "",
    ) -> TaskAcceptResponse:
        """Accept/join a task. Requires bearer_token (or dev mode with agent_id header).

        Args:
            task_id: Task to accept.
            agent_id: Override agent identity (dev mode / X-Creator-Id header).
            agent_name: Optional display name for the agent.
            agent_type: Agent type — "agent" or "human" (default: "agent").
            message: Optional message to the creator.
        """
        headers: dict[str, str] = {}
        if agent_id:
            headers["X-Creator-Id"] = agent_id
        if agent_name:
            headers["X-Creator-Name"] = agent_name
        if agent_type != "agent":
            headers["X-Creator-Type"] = agent_type

        response = await self._client.post(
            f"/api/v1/tasks/{task_id}/accept",
            json={"message": message},
            headers=headers,
        )
        if not response.is_success:
            try:
                error = response.json()
                msg = error.get("detail", response.text)
            except Exception:
                msg = response.text
            raise ACNError(response.status_code, msg)
        return TaskAcceptResponse.model_validate(response.json())

    async def submit_task(
        self,
        task_id: str,
        submission: str,
        participation_id: str | None = None,
        artifacts: list[dict] | None = None,
        agent_id: str | None = None,
    ) -> TaskInfo:
        """Submit task result. Requires bearer_token (or dev mode with agent_id header)."""
        headers: dict[str, str] = {}
        if agent_id:
            headers["X-Creator-Id"] = agent_id

        body: dict[str, Any] = {"submission": submission, "artifacts": artifacts or []}
        if participation_id:
            body["participation_id"] = participation_id

        response = await self._client.post(
            f"/api/v1/tasks/{task_id}/submit",
            json=body,
            headers=headers,
        )
        if not response.is_success:
            try:
                error = response.json()
                msg = error.get("detail", response.text)
            except Exception:
                msg = response.text
            raise ACNError(response.status_code, msg)
        return TaskInfo.model_validate(response.json())

    async def review_task(
        self,
        task_id: str,
        approved: bool,
        notes: str = "",
        participation_id: str | None = None,
        agent_id: str | None = None,
        creator_id: str | None = None,
    ) -> TaskInfo:
        """Approve or reject a task submission. Requires bearer_token (or dev mode)."""
        headers: dict[str, str] = {}
        if creator_id:
            headers["X-Creator-Id"] = creator_id

        body: dict[str, Any] = {"approved": approved, "notes": notes}
        if participation_id:
            body["participation_id"] = participation_id
        if agent_id:
            body["agent_id"] = agent_id

        response = await self._client.post(
            f"/api/v1/tasks/{task_id}/review",
            json=body,
            headers=headers,
        )
        if not response.is_success:
            try:
                error = response.json()
                msg = error.get("detail", response.text)
            except Exception:
                msg = response.text
            raise ACNError(response.status_code, msg)
        return TaskInfo.model_validate(response.json())

    async def cancel_task(self, task_id: str) -> TaskInfo:
        """Cancel a task. Only the task creator (identified by JWT sub) can cancel."""
        response = await self._client.post(
            f"/api/v1/tasks/{task_id}/cancel",
        )
        if not response.is_success:
            try:
                error = response.json()
                msg = error.get("detail", response.text)
            except Exception:
                msg = response.text
            raise ACNError(response.status_code, msg)
        return TaskInfo.model_validate(response.json())

    async def get_participations(self, task_id: str) -> list[ParticipationInfo]:
        """Get all participation records for a task. Requires agent API key auth.

        Submission content is redacted for callers who are neither the task
        creator nor a participant (security M6).
        """
        data = await self._request("GET", f"/api/v1/tasks/{task_id}/participations")
        return [ParticipationInfo.model_validate(p) for p in data.get("participations", [])]

    async def get_my_participation(
        self, task_id: str, agent_id: str | None = None
    ) -> ParticipationInfo | None:
        """Get the current user's participation record for a task."""
        headers: dict[str, str] = {}
        if agent_id:
            headers["X-Creator-Id"] = agent_id

        response = await self._client.get(
            f"/api/v1/tasks/{task_id}/participations/me",
            headers=headers,
        )
        if response.status_code == 404 or response.status_code == 204:
            return None
        if not response.is_success:
            try:
                error = response.json()
                msg = error.get("detail", response.text)
            except Exception:
                msg = response.text
            raise ACNError(response.status_code, msg)
        body = response.json()
        return ParticipationInfo.model_validate(body) if body else None

    async def approve_participation(
        self,
        task_id: str,
        participation_id: str,
        creator_id: str | None = None,
    ) -> TaskInfo:
        """Approve a specific participant for an assigned task (creator only).

        Sets the participant as the task assignee.
        """
        headers: dict[str, str] = {}
        if creator_id:
            headers["X-Creator-Id"] = creator_id

        response = await self._client.post(
            f"/api/v1/tasks/{task_id}/participations/{participation_id}/approve",
            headers=headers,
        )
        if not response.is_success:
            try:
                error = response.json()
                msg = error.get("detail", response.text)
            except Exception:
                msg = response.text
            raise ACNError(response.status_code, msg)
        return TaskInfo.model_validate(response.json())

    async def reject_participation(
        self,
        task_id: str,
        participation_id: str,
        creator_id: str | None = None,
    ) -> TaskInfo:
        """Reject a specific participant's application for an assigned task (creator only)."""
        headers: dict[str, str] = {}
        if creator_id:
            headers["X-Creator-Id"] = creator_id

        response = await self._client.post(
            f"/api/v1/tasks/{task_id}/participations/{participation_id}/reject",
            headers=headers,
        )
        if not response.is_success:
            try:
                error = response.json()
                msg = error.get("detail", response.text)
            except Exception:
                msg = response.text
            raise ACNError(response.status_code, msg)
        return TaskInfo.model_validate(response.json())

    async def cancel_participation(
        self,
        task_id: str,
        participation_id: str,
        agent_id: str | None = None,
    ) -> TaskInfo:
        """Withdraw from a task (participant cancels their own participation).

        Requires bearer_token (or dev mode with agent_id header).
        """
        headers: dict[str, str] = {}
        if agent_id:
            headers["X-Creator-Id"] = agent_id

        response = await self._client.post(
            f"/api/v1/tasks/{task_id}/participations/{participation_id}/cancel",
            headers=headers,
        )
        if not response.is_success:
            try:
                error = response.json()
                msg = error.get("detail", response.text)
            except Exception:
                msg = response.text
            raise ACNError(response.status_code, msg)
        return TaskInfo.model_validate(response.json())

    # ============================================
    # Social Graph (Follow)
    # ============================================

    async def follow(self, agent_id: str, target_id: str) -> dict[str, Any]:
        """Follow another agent.

        Idempotent — re-following an already-followed agent returns 200
        with ``changed=False``.

        Args:
            agent_id: The follower (must match the authenticated agent's ID).
            target_id: The agent to follow.

        Returns:
            ``{ follower_id, followee_id, following, changed }``
        """
        return await self._request(
            "POST", f"/api/v1/agents/{agent_id}/follows/{target_id}"
        )

    async def unfollow(self, agent_id: str, target_id: str) -> dict[str, Any]:
        """Unfollow an agent.

        Idempotent — unfollowing someone you don't follow returns 200
        with ``changed=False``.

        Args:
            agent_id: The follower (must match the authenticated agent's ID).
            target_id: The agent to unfollow.

        Returns:
            ``{ follower_id, followee_id, following, changed }``
        """
        return await self._request(
            "DELETE", f"/api/v1/agents/{agent_id}/follows/{target_id}"
        )

    async def check_follow(self, agent_id: str, target_id: str) -> dict[str, Any]:
        """Check whether ``agent_id`` is following ``target_id`` (public).

        Returns:
            ``{ follower_id, followee_id, following }``
        """
        return await self._request(
            "GET", f"/api/v1/agents/{agent_id}/follows/{target_id}"
        )

    async def list_follows(
        self,
        agent_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AgentInfo]:
        """List agents that ``agent_id`` follows (public).

        Args:
            agent_id: Agent whose following list to fetch.
            limit: Max entries per page (server cap 500).
            offset: Pagination offset.
        """
        data = await self._request(
            "GET",
            f"/api/v1/agents/{agent_id}/follows",
            params={"limit": limit, "offset": offset},
        )
        return [AgentInfo.model_validate(a) for a in data.get("agents", [])]

    async def list_followers(
        self,
        agent_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AgentInfo]:
        """List agents that follow ``agent_id`` (public).

        Args:
            agent_id: Agent whose followers to fetch.
            limit: Max entries per page (server cap 500).
            offset: Pagination offset.
        """
        data = await self._request(
            "GET",
            f"/api/v1/agents/{agent_id}/followers",
            params={"limit": limit, "offset": offset},
        )
        return [AgentInfo.model_validate(a) for a in data.get("agents", [])]

    # ============================================
    # Communication Policy
    # ============================================

    async def get_policy(self, agent_id: str) -> dict[str, Any]:
        """Get the current communication policy for the authenticated agent.

        Args:
            agent_id: Must match the authenticated agent's ID.

        Returns:
            ``{ agent_id, communication_policy: { mode, reject_reason? } }``
        """
        return await self._request("GET", f"/api/v1/agents/{agent_id}/policy")

    async def update_policy(
        self,
        agent_id: str,
        mode: str,
        *,
        reject_reason: str | None = None,
    ) -> dict[str, Any]:
        """Update the agent's inbound communication policy.

        Args:
            agent_id: Must match the authenticated agent's ID.
            mode: ``'open'`` | ``'closed'`` | ``'manifest'`` | ``'allowlist'``
            reject_reason: Optional message shown to rejected senders
                (only meaningful when ``mode='closed'``).

        Returns:
            ``{ agent_id, communication_policy: { mode, reject_reason? }, warning? }``

            ``warning`` is conditionally included when the post-update
            ``mode`` is ``'manifest'`` or ``'allowlist'``. It carries a
            human-readable reminder that messages from non-trusted
            senders divert to the manifest queue and require the agent
            to actively poll ``GET /communication/manifest/{id}`` —
            otherwise those messages expire after the configured TTL
            (default 7 days). Surface this in agent CLIs / dashboards
            so operators don't silently lock themselves out.
        """
        policy: dict[str, Any] = {"mode": mode}
        if reject_reason is not None:
            policy["reject_reason"] = reject_reason
        return await self._request(
            "PATCH",
            f"/api/v1/agents/{agent_id}/policy",
            json={"communication_policy": policy},
        )

    # ============================================
    # Delivery transport (ADR-0012 Mode A / Mode B)
    # ============================================

    async def get_delivery(self, agent_id: str) -> dict[str, Any]:
        """Get the derived inbound delivery transport for this agent.

        Orthogonal to :meth:`get_policy` (reception). Values:

        - ``direct`` — Mode A: ACN dials the public A2A endpoint over HTTP
        - ``relay`` — Mode B: hold an outbound WebSocket (``acn listen``)
        - ``none`` — pull/reject only (policy is ``manifest`` / ``closed``)

        Args:
            agent_id: Must match the authenticated agent's ID.

        Returns:
            ``{ agent_id, delivery, endpoint?, communication_mode }``
        """
        return await self._request("GET", f"/api/v1/agents/{agent_id}/delivery")

    async def set_delivery(
        self,
        agent_id: str,
        delivery: str,
        *,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        """Switch Mode A (direct) ↔ Mode B (relay) without re-registering.

        Requires a push reception policy (``open`` / ``allowlist``). Use
        :meth:`update_policy` first when still on ``manifest``.

        Args:
            agent_id: Must match the authenticated agent's ID.
            delivery: ``'relay'`` or ``'direct'``.
            endpoint: Required for ``direct`` — full public A2A JSON-RPC
                URL. Must be omitted for ``relay``.

        Returns:
            ``{ agent_id, delivery, endpoint?, communication_mode,
            a2a_handshake_ok?, next_step_hint? }``
        """
        normalized = (delivery or "").strip().lower()
        if normalized not in ("direct", "relay"):
            raise ValueError("delivery must be 'direct' or 'relay'")
        if normalized == "relay" and endpoint:
            raise ValueError(
                "delivery='relay' is mutually exclusive with endpoint; "
                "omit endpoint and run acn listen / hold a WebSocket"
            )
        if normalized == "direct" and not endpoint:
            raise ValueError(
                "delivery='direct' requires endpoint "
                "(full A2A URL, e.g. https://host/a2a)"
            )
        body: dict[str, Any] = {"delivery": normalized}
        if endpoint is not None:
            body["endpoint"] = endpoint
        return await self._request(
            "PATCH",
            f"/api/v1/agents/{agent_id}/delivery",
            json=body,
        )

    # ============================================
    # Allowlist
    # ============================================

    async def add_to_allowlist(
        self,
        agent_id: str,
        target_id: str,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Add an agent to the allowlist (owner only).

        Only effective when ``communication_policy.mode = 'allowlist'``.
        Idempotent — re-adding returns ``changed=False``.

        Args:
            agent_id: Must match the authenticated agent's ID.
            target_id: Agent to trust.
            reason: Optional free-form note (≤ 200 chars).

        Returns:
            ``{ agent_id, target_id, allowlisted, changed }``
        """
        body: dict[str, Any] = {}
        if reason is not None:
            body["reason"] = reason
        return await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/allowlist/{target_id}",
            json=body or None,
        )

    async def remove_from_allowlist(
        self, agent_id: str, target_id: str
    ) -> dict[str, Any]:
        """Remove an agent from the allowlist (owner only).

        Idempotent — removing a non-member returns ``changed=False``.

        Args:
            agent_id: Must match the authenticated agent's ID.
            target_id: Agent to remove.

        Returns:
            ``{ agent_id, target_id, allowlisted, changed }``
        """
        return await self._request(
            "DELETE",
            f"/api/v1/agents/{agent_id}/allowlist/{target_id}",
        )

    async def list_allowlist(
        self,
        agent_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List the agent's allowlist (owner only).

        Args:
            agent_id: Must match the authenticated agent's ID.
            limit: Max entries per page (server cap 500).
            offset: Pagination offset.

        Returns:
            List of ``{ target_id, reason?, created_at }`` dicts.
        """
        data = await self._request(
            "GET",
            f"/api/v1/agents/{agent_id}/allowlist",
            params={"limit": limit, "offset": offset},
        )
        entries: list[dict[str, Any]] = data.get("entries", [])
        return entries

    # -------------------------------------------------------------------------
    # ERC-8004 On-Chain Identity
    # -------------------------------------------------------------------------

    async def register_onchain(
        self,
        agent_id: str,
        private_key: str | None = None,
        chain: str = "base",
        rpc_url: str | None = None,
        save_wallet_path: str | None = ".env",
    ) -> dict[str, Any]:
        """Register the agent on ERC-8004 Identity Registry and bind to ACN.

        Handles the full flow:
        1. Generate wallet if private_key is None (saved to save_wallet_path).
        2. Construct agentURI pointing to this agent's agent-registration.json.
        3. Build and sign register(agentURI) transaction.
        4. Broadcast and wait for receipt.
        5. Extract token ID from Registered event.
        6. POST /api/v1/onchain/agents/{agent_id}/bind to inform ACN.

        Args:
            agent_id: ACN agent ID (from join response).
            private_key: Ethereum private key (hex). None = auto-generate.
            chain: Target chain. "base" (mainnet) or "base-sepolia" (testnet).
            rpc_url: Custom RPC URL. Defaults to public endpoint for chain.
            save_wallet_path: File path to save generated wallet. Ignored if
                private_key is provided.

        Returns:
            dict with token_id, tx_hash, chain, agent_registration_url,
            wallet_address.
        """
        try:
            from eth_account import Account  # type: ignore[import-untyped]
            from web3 import Web3  # type: ignore[import-untyped]
        except ImportError as e:
            raise ImportError(
                "web3 is required for on-chain registration. "
                "Install it with: pip install web3"
            ) from e

        # ---- Chain configuration ----
        chain_configs: dict[str, dict[str, Any]] = {
            "base": {
                "rpc": "https://mainnet.base.org",
                "chain_id": 8453,
                "identity_contract": "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432",
                "namespace": "eip155:8453",
            },
            "base-sepolia": {
                "rpc": "https://sepolia.base.org",
                "chain_id": 84532,
                "identity_contract": "0x8004A818BFB912233c491871b3d84c89A494BD9e",
                "namespace": "eip155:84532",
            },
        }
        if chain not in chain_configs:
            raise ValueError(f"Unsupported chain: {chain}. Use 'base' or 'base-sepolia'.")
        cfg = chain_configs[chain]
        effective_rpc = rpc_url or cfg["rpc"]

        # ---- Wallet ----
        wallet_generated = False
        if private_key is None:
            account = Account.create()
            private_key = account.key.hex()
            wallet_address = account.address
            wallet_generated = True
            if save_wallet_path:
                _save_wallet_to_env(save_wallet_path, private_key, wallet_address)
            print(f"Wallet generated: {wallet_address}")
            print(f"  Private key saved to: {save_wallet_path}")
            print("  ⚠  Back up your private key!")
        else:
            account = Account.from_key(private_key)
            wallet_address = account.address

        # ---- agentURI ----
        agent_registration_url = (
            f"{self.base_url}/api/v1/agents/{agent_id}"
            "/.well-known/agent-registration.json"
        )

        # ---- Minimal Identity Registry ABI (register function + Registered event) ----
        identity_abi = [
            {
                "inputs": [{"internalType": "string", "name": "agentURI", "type": "string"}],
                "name": "register",
                "outputs": [{"internalType": "uint256", "name": "agentId", "type": "uint256"}],
                "stateMutability": "nonpayable",
                "type": "function",
            },
            {
                "anonymous": False,
                "inputs": [
                    {
                        "indexed": True,
                        "internalType": "uint256",
                        "name": "agentId",
                        "type": "uint256",
                    },
                    {
                        "indexed": False,
                        "internalType": "string",
                        "name": "agentURI",
                        "type": "string",
                    },
                    {
                        "indexed": True,
                        "internalType": "address",
                        "name": "owner",
                        "type": "address",
                    },
                ],
                "name": "Registered",
                "type": "event",
            },
        ]

        # ---- Build & send transaction ----
        w3 = Web3(Web3.HTTPProvider(effective_rpc))
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(cfg["identity_contract"]),
            abi=identity_abi,
        )

        tx = contract.functions.register(agent_registration_url).build_transaction(
            {
                "from": wallet_address,
                "nonce": w3.eth.get_transaction_count(wallet_address),
                "chainId": cfg["chain_id"],
            }
        )
        signed = account.sign_transaction(tx)
        tx_hash_bytes = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash_bytes)
        tx_hash = receipt["transactionHash"].hex()

        # ---- Extract token ID from Registered event ----
        registered_events = contract.events.Registered().process_receipt(receipt)
        if not registered_events:
            raise RuntimeError("Registered event not found in transaction receipt")
        token_id: int = registered_events[0]["args"]["agentId"]

        # ---- Notify ACN ----
        await self._request(
            "POST",
            f"/api/v1/onchain/agents/{agent_id}/bind",
            json={"token_id": token_id, "chain": cfg["namespace"], "tx_hash": tx_hash},
        )

        print("\nAgent registered on-chain!")
        print(f"  Token ID:         {token_id}")
        print(f"  Tx Hash:          {tx_hash}")
        print(f"  Chain:            {cfg['namespace']}")
        print(f"  Registration URL: {agent_registration_url}")

        return {
            "token_id": token_id,
            "tx_hash": tx_hash,
            "chain": cfg["namespace"],
            "agent_registration_url": agent_registration_url,
            "wallet_address": wallet_address,
            "wallet_generated": wallet_generated,
        }


def _save_wallet_to_env(path: str, private_key: str, address: str) -> None:
    """Append wallet credentials to a .env file (creates if absent)."""
    import os

    lines = []
    if os.path.exists(path):
        with open(path) as f:
            lines = f.readlines()

    keys_to_set = {
        "WALLET_PRIVATE_KEY": private_key,
        "WALLET_ADDRESS": address,
    }
    existing_keys = {line.split("=")[0].strip() for line in lines if "=" in line}

    with open(path, "a") as f:
        for key, value in keys_to_set.items():
            if key not in existing_keys:
                f.write(f"{key}={value}\n")
