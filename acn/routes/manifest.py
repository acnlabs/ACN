"""Manifest Queue API Routes — Phase 2 PR #1

Endpoints under ``/api/v1/communication`` that complement the
manifest-mode policy. Routes here only manage the *queue* itself;
the produce-side (write a manifest entry from an inbound send) lives
on ``MessageRouter._route_to_manifest`` and reuses the existing
``POST /communication/send`` entry point.

API surface (decision Group A #4 + P0-3 auth matrix):

* ``GET /communication/manifest/{agent_id}``
    List the agent's manifest queue (newest-first paging via
    ``since_ms`` cursor). Owner-only (``OwnerOrInternalDep``); 403
    when the path id doesn't match the API-key bearer.

* ``DELETE /communication/manifest/{agent_id}/{mid}``
    Drop a single manifest entry. Owner-only. ``mid`` mismatch
    surfaces 404 (never 403) because the existence of the row is
    itself sensitive.

* ``GET /communication/content/{mid}``
    Pull the full payload for a specific ``mid``. Authenticated as
    an agent (``AgentApiKeyDep``); the recipient's id is taken
    from the API key, not the path, so attempting to read another
    agent's manifest content surfaces 404 (the route layer never
    leaks which other tenant might own the ``mid``).

Rate limits mirror the existing ``/communication/history/{agent_id}``
shapes — list/delete are roughly as cheap as inbox reads, content
fetch is slightly costlier (Redis GET on a JSON blob) but still well
under the per-agent budget.

See docs/features/acn-communication-economic-model.md
"Phase 2 原型 PR 验收清单" for the assertions exercised by the test
suite.
"""

from __future__ import annotations

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, Query, Request

from ..core.errors import ACN_DEFAULT_RESPONSES, ACNHTTPError, ErrorCode
from .dependencies import (
    AgentApiKeyDep,
    AgentIdPath,
    ManifestServiceDep,
    OwnerOrInternalDep,
    limiter,
)

router = APIRouter(
    prefix="/api/v1/communication",
    tags=["manifest"],
    responses=ACN_DEFAULT_RESPONSES,
)
logger = structlog.get_logger()


# Default page size mirrors ``GET /communication/history/{agent_id}``
# so clients can reuse paging code. The hard ceiling (200) matches
# ``QUEUE_CAPACITY`` in ``ManifestService`` so a single page can
# return the entire retained queue without forcing pagination.
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


@router.get("/manifest/{agent_id}")
# Phase 2: this is a low-risk read endpoint (no DB writes, just a
# ZRANGE + HGETALL pipeline) — match the read-side budget on
# ``/history``. 120/min ≈ 2 req/s per agent leaves plenty of room
# for a manifest UI to poll without bumping into the limiter.
@limiter.limit("120/minute")
async def list_manifest(
    request: Request,  # required by slowapi limiter
    agent_id: AgentIdPath,
    caller: OwnerOrInternalDep,
    manifest_service: ManifestServiceDep,
    since_ms: int | None = Query(
        default=None,
        ge=0,
        description="Cursor: only return entries with ts >= since_ms.",
    ),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
):
    """List an agent's manifest queue.

    Returns:
        ``{"agent_id": ..., "entries": [...], "count": N}``. Each
        entry exposes ``mid``, ``sender_id``, ``summary``, ``ts``
        (ms), and ``content_size``. The full payload is *not*
        included; clients pull it via
        ``GET /communication/content/{mid}`` so the listing endpoint
        stays cheap.
    """
    # OwnerOrInternalDep guarantees one of:
    #   * ``caller_kind == "internal"`` (X-Internal-Token), in which
    #     case the platform op tooling is allowed to inspect any
    #     agent's queue.
    #   * ``caller_kind == "agent"`` and ``caller["agent_id"]``
    #     matches the path ``agent_id`` (the dep itself enforces
    #     this — no extra check needed). 403 surface preserved.
    del caller  # all enforcement happens inside the dependency

    entries = await manifest_service.read_since(
        owner_id=agent_id,
        since_ms=since_ms,
        limit=limit,
    )
    return {
        "agent_id": agent_id,
        "count": len(entries),
        "entries": [
            {
                "mid": e.mid,
                "sender_id": e.sender_id,
                "summary": e.summary,
                "ts": e.ts_ms,
                "content_size": e.content_size,
                **({"extra": e.extra} if e.extra else {}),
            }
            for e in entries
        ],
    }


@router.delete("/manifest/{agent_id}/{mid}")
# Mutating endpoint, but cheap (3 DELs in one MULTI). 60/min
# matches the per-agent ``/send`` budget — even an over-zealous
# "clear my manifest" client should be fine.
@limiter.limit("60/minute")
async def delete_manifest_entry(
    request: Request,
    agent_id: AgentIdPath,
    mid: str,
    caller: OwnerOrInternalDep,
    manifest_service: ManifestServiceDep,
):
    """Delete a single manifest entry + its content.

    Returns:
        ``{"deleted": true}`` when the entry existed, 404 otherwise.
        Cross-tenant ``mid``s also surface 404 (we never reveal
        whether the ``mid`` exists for another owner).
    """
    del caller  # OwnerOrInternalDep enforces path/auth match

    deleted = await manifest_service.delete(owner_id=agent_id, mid=mid)
    if not deleted:
        # 404 is intentional even when the issue is cross-tenant:
        # the existence of a manifest entry is itself sensitive,
        # so leaking it via a different status code (e.g. 403)
        # would let an attacker probe other agents' queues.
        raise ACNHTTPError(
            ErrorCode.MANIFEST_ENTRY_NOT_FOUND,
            status_code=404,
            details={"agent_id": agent_id, "mid": mid},
        )
    return {"agent_id": agent_id, "mid": mid, "deleted": True}


@router.get("/content/{mid}")
# Slightly costlier than the listing (single GET of a JSON blob up
# to MAX_CONTENT_BYTES), but still bounded by Redis bandwidth.
# Match the ``/send`` budget — every send produces at most one pull.
@limiter.limit("60/minute")
async def fetch_manifest_content(
    request: Request,
    mid: str,
    agent_info: AgentApiKeyDep,
    manifest_service: ManifestServiceDep,
):
    """Pull the full payload for a manifest entry.

    The recipient is *always* derived from the API key, never from
    the path, so:

    * Cross-tenant attempts (the API-key agent doesn't own ``mid``)
      → 404 (route layer cannot distinguish from "expired").
    * Expired entries → 404.
    * Repeatable: this endpoint has no read-once semantics in
      Phase 2; Phase 3 will introduce an explicit ``ack`` step
      that releases ``attention_fee``.
    """
    owner_id = agent_info["agent_id"]
    payload = await manifest_service.fetch_content(owner_id=owner_id, mid=mid)
    if payload is None:
        raise ACNHTTPError(
            ErrorCode.MANIFEST_CONTENT_NOT_FOUND,
            status_code=404,
            details={"owner_id": owner_id, "mid": mid},
        )
    return {
        "mid": mid,
        "owner_id": owner_id,
        "content": payload,
    }
