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

import base64
from typing import Any

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, Query, Request

from ..core.errors import ACN_DEFAULT_RESPONSES, ACNHTTPError, ErrorCode
from ..services.manifest_service import AlreadyAckedError
from .dependencies import (
    AgentApiKeyDep,
    AgentIdPath,
    EscrowProviderDep,
    ManifestServiceDep,
    OptionalEscrowProviderDep,
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
    type: str | None = Query(
        default=None,
        description=(
            "Filter by message_type. Allowed values: task_request, "
            "collaboration, inquiry, broadcast, session_invite."
        ),
        max_length=64,
    ),
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
        message_type=type,
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
                **({"message_type": e.message_type} if e.message_type else {}),
                # Phase 3: surface acked_at on the listing so the
                # recipient client can colour-code which entries
                # still owe an ack call.
                **({"acked_at": e.acked_at_ms} if e.acked_at_ms is not None else {}),
                **({"extra": e.extra} if e.extra else {}),
            }
            for e in entries
        ],
    }


@router.delete("/manifest/{agent_id}/{mid}")
# Mutating endpoint, but cheap on the no-fee path (3 DELs in one
# MULTI). With an attention_fee the cost grows by one backend
# refund round-trip; keep the same per-agent budget (60/min)
# because the recipient cannot exceed their own send-rate budget
# anyway — a sender cannot generate more paid manifest entries
# than the target can dispose of.
@limiter.limit("60/minute")
async def delete_manifest_entry(
    request: Request,
    agent_id: AgentIdPath,
    mid: str,
    caller: OwnerOrInternalDep,
    manifest_service: ManifestServiceDep,
    escrow_provider: OptionalEscrowProviderDep,
):
    """Delete a single manifest entry + its content (+ refund any locked fee).

    Phase 3 attention_fee semantics:
        When the entry being deleted has a locked ``attention_fee``,
        the recipient is *declining* the message. The locked escrow
        MUST go back to the sender — otherwise the sender's funds
        get stripped without the recipient ever consuming the
        message, breaking the locked-or-released contract that the
        whole attention_fee design rests on.

        Order of operations (refund-first):
        1. Read the entry. If it has ``extra.attention_fee.escrow_id``,
           refund the escrow via ``refund_v2`` BEFORE deleting any
           manifest data.
        2. If the refund call fails, abort the delete (4xx
           ``ATTENTION_FEE_RELEASE_FAILED``). The recipient retries
           later, or ops cancels the escrow manually.
        3. Once refund succeeds (or the entry has no fee), delete
           the manifest entry + content keys atomically.

        Refund-first matters: deleting the manifest first would
        orphan the escrow — we'd lose the escrow_id we needed to
        refund, and the funds would sit locked until the operator
        intervened.

        The ack endpoint guards against double-spend by stamping
        ``acked_at`` first and only then releasing. DELETE skips
        that guard because:
        - DELETE is owner-only (OwnerOrInternalDep), so cross-tenant
          replays are already 403.
        - The recipient's own client racing two DELETE calls just
          means the first one wins — backend's refund_v2 returns
          409 on a second attempt (escrow already REFUNDED), which
          we surface as ATTENTION_FEE_RELEASE_FAILED 400. The
          manifest-side keys may already be gone, in which case
          the second call hits 404 first and never reaches
          refund_v2 — same external surface either way.

    Returns:
        ``{"deleted": true}`` when the entry existed, 404 otherwise.
        Cross-tenant ``mid``s also surface 404 (we never reveal
        whether the ``mid`` exists for another owner).
    """
    del caller  # OwnerOrInternalDep enforces path/auth match

    # Pre-fetch so we can detect attention_fee escrows that need to be
    # refunded before we drop the row. ``get_entry`` already returns
    # ``None`` for cross-tenant or expired entries — we forward that
    # to the same 404 used below.
    entry = await manifest_service.get_entry(owner_id=agent_id, mid=mid)
    if entry is None:
        raise ACNHTTPError(
            ErrorCode.MANIFEST_ENTRY_NOT_FOUND,
            status_code=404,
            details={"agent_id": agent_id, "mid": mid},
        )

    attn = entry.extra.get("attention_fee") if entry.extra else None
    locked_escrow_id: str | None = None
    if isinstance(attn, dict) and attn.get("escrow_id"):
        # Only refund if the entry hasn't already been ack-released.
        # ``acked_at_ms`` set means the funds already moved to the
        # recipient via the ack path, so DELETE is just cleanup —
        # no refund needed.
        if entry.acked_at_ms is None:
            locked_escrow_id = str(attn["escrow_id"])

    if locked_escrow_id is not None:
        # Paid manifest entry but the deployment isn't running with
        # an escrow backend wired — surface as 503 (same shape as
        # the ack endpoint's strict dep) rather than silently
        # dropping the entry without a refund.
        if escrow_provider is None:
            raise ACNHTTPError(
                ErrorCode.ATTENTION_FEE_RELEASE_FAILED,
                status_code=503,
                details={
                    "agent_id": agent_id,
                    "mid": mid,
                    "reason": (
                        "escrow provider not configured; cannot refund "
                        "attention_fee without a backend"
                    ),
                    "operation": "refund",
                },
            )
        refund_result = await escrow_provider.refund_v2(
            escrow_id=locked_escrow_id,
            reason=f"acn manifest delete mid={mid}",
        )
        if not refund_result.success:
            raise ACNHTTPError(
                ErrorCode.ATTENTION_FEE_RELEASE_FAILED,
                status_code=400,
                details={
                    "agent_id": agent_id,
                    "mid": mid,
                    "reason": refund_result.error or "unknown error",
                    "operation": "refund",
                },
            )
        logger.info(
            "attention_fee_refunded_on_delete",
            agent_id=agent_id,
            mid=mid,
            escrow_id=locked_escrow_id,
        )

    deleted = await manifest_service.delete(owner_id=agent_id, mid=mid)
    if not deleted:
        # 404 is intentional even when the issue is cross-tenant:
        # the existence of a manifest entry is itself sensitive,
        # so leaking it via a different status code (e.g. 403)
        # would let an attacker probe other agents' queues.
        # This branch is also the race-loser when two DELETEs land
        # concurrently: the first one already refunded + deleted,
        # the second sees ``get_entry → None`` above OR ``delete →
        # False`` here. Either way the externally observable result
        # is 404, which is correct.
        raise ACNHTTPError(
            ErrorCode.MANIFEST_ENTRY_NOT_FOUND,
            status_code=404,
            details={"agent_id": agent_id, "mid": mid},
        )
    response: dict[str, Any] = {"agent_id": agent_id, "mid": mid, "deleted": True}
    if locked_escrow_id is not None:
        response["attention_fee"] = {
            "escrow_id": locked_escrow_id,
            "refunded": True,
        }
    return response


@router.post("/manifest/{agent_id}/{mid}/ack")
# Phase 3 attention_fee release. Cost profile sits between read and
# write: we do one HSETNX, one HGETALL, and (if a fee was attached)
# one backend HTTP round-trip to release_partial. Match the
# ``/send`` budget — the ack is the recipient-side counterpart of
# the per-agent send rate, and a single sender's bursty fan-out is
# already capped by the send rate on the producer side.
@limiter.limit("60/minute")
async def ack_manifest_entry(
    request: Request,
    agent_id: AgentIdPath,
    mid: str,
    caller: OwnerOrInternalDep,
    manifest_service: ManifestServiceDep,
    escrow_provider: EscrowProviderDep,
):
    """Acknowledge a manifest entry and release its attention_fee.

    Owner-only (or internal). The ack call is the recipient's
    explicit signal that they consumed the message; only at this
    point do we release the locked attention_fee from escrow into
    the recipient's wallet. Designed to be idempotent on the
    recipient side: a replay of the same ack returns 4xx
    ``ATTENTION_FEE_ALREADY_ACKED`` so SDK clients can suppress
    duplicates without surfacing a noisy error.

    Request body is intentionally empty — the manifest entry's
    own metadata (escrow_id, amount, currency) plus the caller's
    identity (the recipient agent) are sufficient to drive the
    release. We don't accept caller-supplied "release amount"
    overrides because that would let a recipient release less
    than the sender locked (giving change back at the sender's
    expense, which violates the locked-amount contract).

    Failure modes (each surfaces as 4xx with a distinct error_code):

    * Entry not found / wrong owner → 404 ``MANIFEST_ENTRY_NOT_FOUND``
      (intentional — leaks no information about other tenants' mids).
    * No fee attached → 400 ``ATTENTION_FEE_NOT_LOCKED`` (caller
      should use ``GET /communication/content`` instead).
    * Already acked → 400 ``ATTENTION_FEE_ALREADY_ACKED``.
    * Backend escrow rejected release → 400
      ``ATTENTION_FEE_RELEASE_FAILED`` (with backend's reason in
      ``details``). The manifest entry's ``acked_at`` is rolled
      back so the SDK can retry.
    """
    del caller  # OwnerOrInternalDep enforces path/auth match

    entry = await manifest_service.get_entry(owner_id=agent_id, mid=mid)
    if entry is None:
        raise ACNHTTPError(
            ErrorCode.MANIFEST_ENTRY_NOT_FOUND,
            status_code=404,
            details={"agent_id": agent_id, "mid": mid},
        )

    attn = entry.extra.get("attention_fee") if entry.extra else None
    if not isinstance(attn, dict) or not attn.get("escrow_id"):
        raise ACNHTTPError(
            ErrorCode.ATTENTION_FEE_NOT_LOCKED,
            status_code=400,
            details={"agent_id": agent_id, "mid": mid},
        )

    # HSETNX-based acked_at stamp. Returns the freshly stamped
    # timestamp on the first ack, raises AlreadyAckedError on a
    # replay, returns ``None`` on the cold "entry vanished between
    # get_entry and HSETNX" path. ``None`` is mapped to 404 below
    # for the same reason as the initial fetch — race with TTL
    # eviction or DELETE.
    try:
        acked_at_ms = await manifest_service.mark_acked(
            owner_id=agent_id, mid=mid
        )
    except AlreadyAckedError as exc:
        raise ACNHTTPError(
            ErrorCode.ATTENTION_FEE_ALREADY_ACKED,
            status_code=400,
            details={"agent_id": agent_id, "mid": mid},
        ) from exc
    if acked_at_ms is None:
        raise ACNHTTPError(
            ErrorCode.MANIFEST_ENTRY_NOT_FOUND,
            status_code=404,
            details={"agent_id": agent_id, "mid": mid},
        )

    # We hold the ack token at this point — release the locked
    # funds. The release ID stays the escrow's ``escrow_id`` (not
    # ``task_id``); ``release_partial`` resolves wallets entirely
    # from the escrow record.
    release_amount = int(attn.get("amount") or 0)
    release_result = await escrow_provider.release_partial(
        escrow_id=str(attn["escrow_id"]),
        recipient_id=agent_id,
        recipient_type="agent",
        amount=release_amount,
        notes=f"acn attention_fee ack mid={mid}",
    )
    if not release_result.success:
        # Roll back the acked_at stamp so the SDK can retry the ack
        # without immediately tripping ATTENTION_FEE_ALREADY_ACKED.
        # Best-effort: a backend that succeeds but loses the response
        # would leave the entry with acked_at set + funds released,
        # and the SDK would correctly observe ALREADY_ACKED on
        # retry. The reverse failure mode — ack stamp set but funds
        # not released — is the one we explicitly avoid here.
        try:
            await manifest_service.unmark_acked(owner_id=agent_id, mid=mid)
        except Exception:  # noqa: BLE001
            logger.warning(
                "ack_rollback_failed",
                agent_id=agent_id,
                mid=mid,
            )
        raise ACNHTTPError(
            ErrorCode.ATTENTION_FEE_RELEASE_FAILED,
            status_code=400,
            details={
                "agent_id": agent_id,
                "mid": mid,
                "reason": release_result.error or "unknown error",
                "operation": "release",
            },
        )

    logger.info(
        "attention_fee_released",
        agent_id=agent_id,
        mid=mid,
        escrow_id=attn["escrow_id"],
        amount=release_amount,
        receipt_id=release_result.proof,
    )

    return {
        "agent_id": agent_id,
        "mid": mid,
        "acked": True,
        "acked_at": acked_at_ms,
        "attention_fee": {
            "escrow_id": attn["escrow_id"],
            "currency": attn.get("currency"),
            "amount": release_amount,
            "agent_amount": release_result.agent_amount,
            "acn_amount": release_result.acn_amount,
            "provider_amount": release_result.provider_amount,
            "receipt_id": release_result.proof,
        },
    }


# Default chunk size for cursor-based content pagination (Phase 3).
# 16 KB balances bandwidth (most manifest content < 64 KB) and
# latency (< 4 round-trips for max-size entries). Configurable via
# environment but constant per process to keep cursor math simple.
_CONTENT_CHUNK_SIZE = 16 * 1024  # 16 KB


def _encode_cursor(offset: int) -> str:
    """Encode a byte offset as a URL-safe base64 string cursor token."""
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def _decode_cursor(cursor: str) -> int:
    """Decode a cursor token back to a byte offset.

    Raises ``ValueError`` when the token is not a valid base64url-
    encoded integer, so the route can map it to 400.
    """
    try:
        return int(base64.urlsafe_b64decode(cursor + "==").decode())
    except Exception as exc:
        raise ValueError(f"invalid cursor token: {cursor!r}") from exc


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
    cursor: str | None = Query(
        default=None,
        description=(
            "Pagination cursor returned by a previous response as ``next_cursor``. "
            "Omit to start from the beginning. Only applies to ACN-hosted content; "
            "self-hosted entries are always returned in a single response."
        ),
    ),
):
    """Pull the payload (or self-hosted pointer) for a manifest entry.

    The recipient is *always* derived from the API key, never from
    the path, so:

    * Cross-tenant attempts (the API-key agent doesn't own ``mid``)
      → 404 (route layer cannot distinguish from "expired").
    * Expired entries → 404.
    * Repeatable: no read-once semantics; ``ack`` is the explicit
      release signal for ``attention_fee``.

    **Cursor pagination (Phase 3)**:
    ACN-hosted entries up to 64 KB can be retrieved in chunks.
    Each response includes ``has_more`` and, when ``True``, a
    ``next_cursor`` token to pass as ``?cursor=`` on the next call.
    Clients can stop early (e.g. after reading enough context to
    decide whether to ack) without fetching the full payload.

    **Self-hosted content (Phase 3)**:
    When the sender supplied ``content_url``, ACN never stored the
    body locally. This endpoint returns ``{"self_hosted": true,
    "content_url": ..., "content_hash": ...}`` (``has_more: false``)
    so the recipient can fetch the content directly from the sender's
    server and verify it with the provided hash.
    """
    owner_id = agent_info["agent_id"]

    # Fast-path: check the entry metadata for a self-hosted URL
    # BEFORE trying to read the (non-existent) content key.
    entry = await manifest_service.get_entry(owner_id=owner_id, mid=mid)
    if entry is None:
        raise ACNHTTPError(
            ErrorCode.MANIFEST_CONTENT_NOT_FOUND,
            status_code=404,
            details={"owner_id": owner_id, "mid": mid},
        )

    if entry.content_url:
        # Self-hosted: cursor is meaningless (no ACN-side blob).
        result: dict[str, Any] = {
            "mid": mid,
            "owner_id": owner_id,
            "self_hosted": True,
            "has_more": False,
            "content_url": entry.content_url,
        }
        if entry.content_hash:
            result["content_hash"] = entry.content_hash
        return result

    # Decode cursor → byte offset.
    offset = 0
    if cursor is not None:
        try:
            offset = _decode_cursor(cursor)
        except ValueError as e:
            raise ACNHTTPError(
                ErrorCode.INVALID_REQUEST,
                status_code=400,
                details={"cursor": cursor, "reason": "invalid cursor token"},
            ) from e
        if offset < 0:
            raise ACNHTTPError(
                ErrorCode.INVALID_REQUEST,
                status_code=400,
                details={"cursor": cursor, "reason": "cursor offset must be >= 0"},
            )

    # ACN-hosted path: chunked fetch.
    chunk_result = await manifest_service.fetch_content_chunk(
        owner_id=owner_id,
        mid=mid,
        offset=offset,
        size=_CONTENT_CHUNK_SIZE,
    )
    if chunk_result is None:
        raise ACNHTTPError(
            ErrorCode.MANIFEST_CONTENT_NOT_FOUND,
            status_code=404,
            details={"owner_id": owner_id, "mid": mid},
        )

    chunk_bytes, has_more = chunk_result
    response: dict[str, Any] = {
        "mid": mid,
        "owner_id": owner_id,
        "has_more": has_more,
        "content_chunk": chunk_bytes.decode("utf-8", errors="replace"),
    }
    if has_more:
        response["next_cursor"] = _encode_cursor(offset + len(chunk_bytes))
    return response
