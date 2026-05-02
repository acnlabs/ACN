"""Allowlist Routes (Phase 2 PR #2).

Three endpoints, owner-authenticated, that maintain an agent's
``communication_policy.mode=allowlist`` trust list::

    POST   /api/v1/agents/{agent_id}/allowlist/{target_id}   # owner API key
    DELETE /api/v1/agents/{agent_id}/allowlist/{target_id}   # owner API key
    GET    /api/v1/agents/{agent_id}/allowlist               # owner API key

There is **no public read** endpoint and no "incoming allowlist"
endpoint (where target_id queries who has them). The proposal
treats allowlist membership as a privacy-sensitive trust signal:
exposing "who trusts whom" publicly would leak relationship
information that the recipient may not want disclosed. Even the
``GET`` listing is owner-only — the recipient sees their own list
but no one else can. This mirrors the proposal's "不提供 GET
/allowlist/incoming" decision.

Routing precedence: included in ``api.py`` BEFORE
``registry.router`` (registry has a catch-all
``/{agent_id}/{rest_path:path}`` proxy that would otherwise
swallow these sub-paths). Same pattern follow.py and manifest.py
established.

API shape mirrors follow.py: a ``ChangedResponse`` with
``allowlisted`` (post-state boolean) + ``changed`` (whether THIS
call mutated state). 200 on idempotent re-add / re-remove.
Errors:

  - 400 SelfAllowlistError — recipient cannot allowlist themselves.
  - 403 caller != path agent_id — owner-only auth check.
  - 404 target_id unknown — same shape as follow's "follow a
    non-existent agent" path.
  - 429 capacity exceeded (MAX_ALLOWLIST_SIZE = 500).
"""

from __future__ import annotations

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from ..core.errors import ACNHTTPError, ErrorCode
from ..core.exceptions import AgentNotFoundException
from ..services import (
    AllowlistCapacityExceededError,
    SelfAllowlistError,
)
from ..services.allowlist_service import MAX_ALLOWLIST_SIZE
from .dependencies import (
    AgentApiKeyDep,
    AgentIdPath,
    AllowlistServiceDep,
    limiter,
)

router = APIRouter(prefix="/api/v1/agents", tags=["allowlist"])
logger = structlog.get_logger()


# Same pagination caps as follows.py — keeps API ergonomics uniform
# between the two graph-shaped resources (follows + allowlist) and
# bounds worst-case payload size against the per-owner
# ``MAX_ALLOWLIST_SIZE`` ceiling. With 500 max members per owner,
# a single page covers the entire list.
_MAX_PAGE_LIMIT: int = 500
_DEFAULT_PAGE_LIMIT: int = 100

# Maximum length of the optional ``reason`` body field on POST.
# AllowlistService also clips at 200 chars at write time (defence
# in depth); validating here gives the caller a clean 422 instead
# of a silent truncation surprise. Mirrors the manifest-summary
# cap pattern from PR #1.
_MAX_REASON_LEN: int = 200


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class AddAllowlistBody(BaseModel):
    """Optional body for POST — only carries the free-form reason note.

    The body is *optional* (the route accepts None) so the simplest
    "allowlist this id" call is a single POST with no body. We
    deliberately don't put ``target_id`` in the body — the path
    parameter is the identity of the resource, conventional REST.
    """

    reason: str | None = Field(
        default=None,
        max_length=_MAX_REASON_LEN,
        description=(
            "Free-form note (≤ 200 chars). Surfaced in the owner's "
            "GET listing for context ('trusted partner X'); never "
            "shared with target_id. Optional."
        ),
    )


class AllowlistActionResponse(BaseModel):
    """Result of a POST or DELETE mutation.

    Field naming mirrors ``FollowActionResponse`` from follows.py
    so clients learn one mental model for both graph-mutating
    routes:

    * ``allowlisted`` reflects the post-state boolean (True after
      successful POST, False after DELETE) — drives the UI button.
    * ``changed`` reports whether THIS call mutated server state.
      False on idempotent re-add / re-remove. ``200 changed=false``
      is preferred over ``409 conflict`` because clients can write
      retry-safe code without first GET-ing.
    """

    owner_id: str
    target_id: str
    allowlisted: bool
    changed: bool = Field(
        default=False,
        description=(
            "True only when this call mutated state (POST that "
            "newly added a target, or DELETE that actually removed "
            "one). Repeat-add / repeat-remove returns 200 with "
            "False — idempotent."
        ),
    )


class AllowlistEntryResponse(BaseModel):
    """A single allowlist entry as returned by GET listing.

    ``reason`` is owner-only context; ``created_at`` lets clients
    sort or display "how long has X been trusted" without an extra
    derived field.
    """

    target_id: str
    created_at: str = Field(
        description="ISO-8601 UTC timestamp of when the entry was added."
    )
    reason: str | None = None


class AllowlistListResponse(BaseModel):
    """Paginated GET response.

    ``total`` is the full count regardless of pagination so clients
    can size their pager controls without a second request. Total
    is bounded by ``MAX_ALLOWLIST_SIZE`` (500) so the cost of
    counting is O(1) on the cache layer.
    """

    owner_id: str
    entries: list[AllowlistEntryResponse]
    total: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_owner(caller: dict, agent_id: str) -> None:
    """Allowlist routes are owner-only.

    Centralised here so the same 403 message shape covers all
    three routes; the GET listing also goes through this gate
    because the owner's allowlist is private (see module docstring).
    """
    if caller["agent_id"] != agent_id:
        raise ACNHTTPError(
            ErrorCode.API_KEY_AGENT_MISMATCH,
            403,
            details={
                "path_agent": agent_id,
                "key_agent": caller["agent_id"],
            },
        )


# ---------------------------------------------------------------------------
# Mutation endpoints — owner API key required
# ---------------------------------------------------------------------------


@router.post(
    "/{agent_id}/allowlist/{target_id}",
    response_model=AllowlistActionResponse,
    summary="Add an agent to this agent's allowlist",
)
@limiter.limit("60/minute")
async def add_to_allowlist(
    request: Request,
    agent_id: AgentIdPath,
    target_id: AgentIdPath,
    caller: AgentApiKeyDep,
    allowlist_service: AllowlistServiceDep,
    body: AddAllowlistBody | None = None,
):
    """Add ``target_id`` to ``agent_id``'s allowlist.

    Idempotent: re-adding an already-allowlisted target succeeds
    with ``changed=false`` (no 409). Capacity is enforced at the
    service layer (``MAX_ALLOWLIST_SIZE``); attempting to add the
    501st distinct target returns 429 — matches the proposal's
    "超出返回 429" convention used by follow's per-agent cap.

    Notes on body handling: FastAPI passes ``body=None`` when the
    client sent no body at all, which is the common case (just a
    POST with the path parameters). When a body IS present, only
    the ``reason`` field is read; unknown extra fields are
    rejected by Pydantic (default ``Extra.forbid`` is not on, but
    the model has only one field so the surface is small enough
    to trust).
    """
    _ensure_owner(caller, agent_id)
    reason = body.reason if body is not None else None

    try:
        created = await allowlist_service.add(
            owner_id=agent_id,
            target_id=target_id,
            reason=reason,
        )
    except SelfAllowlistError as exc:
        raise ACNHTTPError(
            ErrorCode.SELF_ALLOWLIST_FORBIDDEN,
            400,
            details={"owner_id": agent_id},
        ) from exc
    except AgentNotFoundException as exc:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": target_id},
        ) from exc
    except AllowlistCapacityExceededError as exc:
        raise ACNHTTPError(
            ErrorCode.ALLOWLIST_CAPACITY_EXCEEDED,
            429,
            details={
                "owner_id": agent_id,
                "max_size": MAX_ALLOWLIST_SIZE,
            },
        ) from exc

    return AllowlistActionResponse(
        owner_id=agent_id,
        target_id=target_id,
        allowlisted=True,
        changed=created,
    )


@router.delete(
    "/{agent_id}/allowlist/{target_id}",
    response_model=AllowlistActionResponse,
    summary="Remove an agent from this agent's allowlist",
)
@limiter.limit("60/minute")
async def remove_from_allowlist(
    request: Request,
    agent_id: AgentIdPath,
    target_id: AgentIdPath,
    caller: AgentApiKeyDep,
    allowlist_service: AllowlistServiceDep,
):
    """Drop ``target_id`` from ``agent_id``'s allowlist.

    Idempotent: returns 200 with ``changed=false`` if no edge
    existed. The service layer's dual-write order (Redis SREM
    first, then PG DELETE) guarantees that a freshly-revoked
    sender cannot keep getting trusted via stale cache — see
    ``AllowlistService.remove`` docstring for the safety
    reasoning.

    Note: this DOES NOT clean up the historical inbox / manifest
    that the (formerly trusted) target may have written before
    revocation. The proposal explicitly retains those messages
    (PR #2 plan P1-8 decision: revocation is forward-looking, like
    flipping the policy mode itself). The recipient still has the
    audit trail of what was sent during the trust window.
    """
    _ensure_owner(caller, agent_id)
    removed = await allowlist_service.remove(
        owner_id=agent_id,
        target_id=target_id,
    )
    return AllowlistActionResponse(
        owner_id=agent_id,
        target_id=target_id,
        allowlisted=False,
        changed=removed,
    )


# ---------------------------------------------------------------------------
# Query endpoint — owner-only (privacy)
# ---------------------------------------------------------------------------


@router.get(
    "/{agent_id}/allowlist",
    response_model=AllowlistListResponse,
    summary="List this agent's allowlist (owner-only)",
)
@limiter.limit("60/minute")
async def list_allowlist(
    request: Request,
    agent_id: AgentIdPath,
    caller: AgentApiKeyDep,
    allowlist_service: AllowlistServiceDep,
    limit: int = Query(
        default=_DEFAULT_PAGE_LIMIT,
        ge=1,
        le=_MAX_PAGE_LIMIT,
        description=f"Max items to return (1..{_MAX_PAGE_LIMIT}).",
    ),
    offset: int = Query(default=0, ge=0, description="Pagination offset."),
):
    """Return the agents on ``agent_id``'s allowlist (newest first).

    Owner-only — the listing leaks "who I trust", which is a
    privacy-sensitive trust signal we choose to keep private.
    Listings always come from the canonical PG side (the Redis
    cache lacks ``created_at`` / ``reason``).
    """
    _ensure_owner(caller, agent_id)
    entries = await allowlist_service.list_targets(
        owner_id=agent_id,
        limit=limit,
        offset=offset,
    )
    total = await allowlist_service.count(agent_id)
    return AllowlistListResponse(
        owner_id=agent_id,
        entries=[
            AllowlistEntryResponse(
                target_id=e.target_id,
                created_at=e.created_at.isoformat(),
                reason=e.reason,
            )
            for e in entries
        ],
        total=total,
    )
