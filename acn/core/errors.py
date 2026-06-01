"""ACN error code catalog and HTTP error transport.

Phase 2 review v2 P1 #11 — pilot: communication routes.

This module is the single source of truth for the **stable ASCII error
code contract** that ACN-emitted error responses surface to SDK clients.
It complements ``acn.core.exceptions`` (domain-layer business
exceptions, no HTTP knowledge) by providing the HTTP / SDK-facing
transport: an enum of stable error codes, a Pydantic response shape,
and an ``ACNHTTPError`` exception that the central handler in
``acn.api`` translates into a flat JSON body.

## Design rationale

### Why a separate module from ``core/exceptions``?

``core/exceptions`` is the domain layer — it must not depend on
Starlette / FastAPI / Pydantic in a circular way (the repository,
service, and route layers all import it). HTTP / SDK contract concerns
belong one layer up; keeping them in ``acn.core.errors`` lets domain
exceptions stay HTTP-agnostic while still mapping cleanly to HTTP
codes at the route boundary.

### Why a flat schema with both 4xx and 5xx aligned?

ACN's 5xx handler (``acn/api.py::_http_exception_handler``) already
emits a flat sanitised body; the 4xx surface was historically the
inconsistent half. With a unified ``{error_code, message, details,
request_id}`` shape, an SDK can write **one** parser for every error
the ACN backend emits — pre-#11 it had to branch on the presence of
``detail`` (FastAPI nested) vs ``error`` (sanitised 5xx).

### Why force ``ACNHTTPError`` to 4xx only?

5xx responses are sanitised by the existing handler chain to prevent
information disclosure (DB error messages, internal hostnames,
tracebacks). If ``ACNHTTPError`` accepted 5xx status codes, a route
author could accidentally bypass that sanitisation by passing a raw
internal error message in ``message=...``. We enforce 4xx at
construction time and route 5xx through the existing
``raise HTTPException(500, str(e))`` path, which the 5xx handler
sanitises before it leaves the process.

### Forward-catalog discipline

The ``ErrorCode`` enum is a forward catalog — it intentionally
contains codes that are *reserved* for future routes (e.g.
``SUBNET_NOT_FOUND`` is declared here even though only the
communication pilot routes use ``AGENT_NOT_FOUND`` and
``COMMUNICATION_REJECTED`` today). The contract enforced by the test
suite is ``set(_DEFAULT_MESSAGES) == set(ErrorCode)``: every declared
code has a default human-readable prose message. We deliberately do
**not** test ``every code is raised by some route``, because that
would couple catalog evolution to the migration sprint and break the
small-PR cadence.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ErrorCode(StrEnum):
    """Stable ASCII error code identifiers.

    Naming convention: ``<resource>_<verb_or_state>`` in snake_case.
    Once a code is published, **renaming is a breaking change** for
    any SDK that pinned a branch on it — treat additions as easy and
    renames as hard.

    Members are visually grouped into three sections — *pilot use*,
    *5xx fallback*, and *reserved* — to keep the catalog readable as
    it grows. The grouping is comment-only; ``StrEnum`` member order
    has no runtime semantics for any current consumer (the test suite
    asserts ``set(_DEFAULT_MESSAGES) == set(ErrorCode)``).
    """

    # ===== Pilot use (communication routes — Phase 2 review v2 P1 #11) =====
    AGENT_NOT_FOUND = "agent_not_found"
    API_KEY_AGENT_MISMATCH = "api_key_agent_mismatch"
    FROM_AGENT_MISMATCH = "from_agent_mismatch"
    COMMUNICATION_REJECTED = "communication_rejected"
    UNKNOWN_STRATEGY = "unknown_strategy"

    # ===== Allowlist routes (sprint row #1) =====
    # ``AGENT_NOT_FOUND`` and ``API_KEY_AGENT_MISMATCH`` above are
    # also reused here; only the allowlist-specific codes appear
    # in this group.
    ALLOWLIST_CAPACITY_EXCEEDED = "allowlist_capacity_exceeded"
    SELF_ALLOWLIST_FORBIDDEN = "self_allowlist_forbidden"

    # ===== Subnets routes (sprint rows #3 + #3-followup) =====
    # ``AGENT_NOT_FOUND`` and ``API_KEY_AGENT_MISMATCH`` above are also
    # raised by ``subnets`` — only the subnet-specific code appears in
    # this group. ``OWNERSHIP_MISMATCH``, ``NOT_SUBNET_MEMBER``,
    # ``AUTHENTICATION_REQUIRED``, and ``INVALID_REQUEST`` (used by the
    # owner-filter / private-subnet / create_subnet gates) live in the
    # cross-module group below.
    SUBNET_NOT_FOUND = "subnet_not_found"

    # ===== Subnet admission (ADR-0004 Slice 2.3) =====
    # All twelve codes are emitted by the join-flow / allowlist /
    # invitation / join_request endpoints introduced in Slice 2.3. The
    # split into per-row-kind codes (JOIN_REQUEST_* vs INVITATION_*) is
    # deliberate: ADR-0004 §"URL alias routing rules" routes
    # ``/join-requests/{id}`` and ``/invitations/{id}`` against the
    # same ``subnet_join_requests`` table but uses path-namespace 404s
    # to avoid leaking the existence of a row in the other namespace
    # (calling the join_request approve verb against an invitation id
    # MUST 404 with ``JOIN_REQUEST_NOT_FOUND`` regardless of whether
    # an invitation with that id happens to exist). Sharing one
    # ``ROW_NOT_FOUND`` code would let SDK clients infer the leakage.
    # See ADR §"HTTP status code conventions" for the status mapping.
    SUBNET_NOT_OWNER = "subnet_not_owner"
    NOT_INVITEE = "not_invitee"
    ALREADY_MEMBER = "already_member"
    ALREADY_ON_ALLOWLIST = "already_on_allowlist"
    JOIN_REQUEST_NOT_FOUND = "join_request_not_found"
    JOIN_REQUEST_PENDING = "join_request_pending"
    JOIN_REQUEST_ALREADY_DECIDED = "join_request_already_decided"
    INVITATION_NOT_FOUND = "invitation_not_found"
    INVITATION_PENDING = "invitation_pending"
    INVITATION_ALREADY_DECIDED = "invitation_already_decided"
    INVALID_KIND_FILTER = "invalid_kind_filter"
    # ``visibility_policy_conflict`` (``is_private=true`` +
    # ``join_policy='open'``) stays surfaced under the generic
    # ``INVALID_REQUEST`` ErrorCode with ``details.reason=
    # "visibility_policy_conflict"`` — established by Slice 2.0/2.1
    # and pinned in test_subnets_join_policy.py. Adding a dedicated
    # slug here would silently break SDK clients that already
    # branch on the ``invalid_request`` code; treat it as a
    # ``details.reason`` discriminator instead.

    # ===== Tasks routes (sprint rows #4 + #4-followup) =====
    # ``TASK_NOT_FOUND`` is the only tasks-specific code; the remaining
    # 26 4xx sites (auth / permission / validation / private-subnet
    # gate) pick up cross-module codes from the section below.
    TASK_NOT_FOUND = "task_not_found"

    # ===== Follows routes (sprint row #6) =====
    # ``AGENT_NOT_FOUND`` (×1 — followee lookup miss) and
    # ``API_KEY_AGENT_MISMATCH`` (×2 — follow / unfollow path-mismatch
    # gates) are reused from the pilot group; the two codes below are
    # follows-specific. Naming mirrors sprint #1 ``allowlist`` (which
    # has the same shape: a per-agent capacity ceiling + a
    # self-reference forbidden) but uses ``follower_id`` instead of
    # ``owner_id`` because follow has no ownership semantics — the
    # service-layer exception names (``FollowLimitExceededError``,
    # ``SelfFollowError``) and the ``acn-follow-proposal.md`` response
    # bodies all use ``follower`` as the entity.
    FOLLOW_LIMIT_EXCEEDED = "follow_limit_exceeded"
    SELF_FOLLOW_FORBIDDEN = "self_follow_forbidden"

    # ===== Manifest routes (sprint row #8) =====
    # ``manifest.py`` exposes two distinct 404 surfaces — the
    # listing/delete endpoints (``DELETE /communication/manifest/
    # {agent_id}/{mid}``) operate on the ZSET entry, while the
    # content-fetch endpoint (``GET /communication/content/{mid}``)
    # operates on the JSON blob keyed by ``mid``. Splitting them as
    # distinct codes (vs a single ``MANIFEST_NOT_FOUND`` with
    # ``details.kind ∈ {entry, content}``) lets SDK clients branch
    # without inspecting ``details``, and gives the cross-sprint
    # consistency test strict (not union-schema) protection on each
    # code's ``details`` shape.
    #
    # Cross-tenant access also surfaces the same code (404 — the
    # existence of an entry/content for another owner is itself
    # sensitive and the route layer never leaks it via a different
    # status code). See ``manifest.py`` per-route docstrings.
    MANIFEST_ENTRY_NOT_FOUND = "manifest_entry_not_found"
    MANIFEST_CONTENT_NOT_FOUND = "manifest_content_not_found"
    INBOX_MESSAGE_NOT_FOUND = "inbox_message_not_found"

    # ===== Session layer (Phase 3) =====
    SESSION_NOT_FOUND = "session_not_found"
    SESSION_ALREADY_ACCEPTED = "session_already_accepted"
    SESSION_EXPIRED = "session_expired"
    SESSION_FORBIDDEN = "session_forbidden"

    # ===== Attention fee (Phase 3 — manifest economics) =====
    # ``attention_fee`` is the sender-pays-recipient mechanism that turns
    # the manifest queue from a free best-effort firehose into a
    # signal-quality channel: a sender attaches a small Credits amount
    # at ``POST /communication/send`` time, the funds are locked in
    # escrow, and they release to the recipient when (and only when)
    # the recipient explicitly acks the manifest entry. Refunds back
    # to the sender on TTL expiry / manual delete.
    #
    # Six codes — each maps to a *distinct caller-facing failure mode*
    # at a different stage of the lock → ack → release pipeline. We do
    # not collapse them into a single ``ATTENTION_FEE_FAILURE`` because
    # SDK clients want to branch:
    #
    # * ``ATTENTION_FEE_INVALID`` — schema/range violation (negative
    #   amount, currency not in ``{credits}``, amount above the per-fee
    #   ceiling). Caller's next step: fix the request body.
    # * ``ATTENTION_FEE_REQUIRES_MANIFEST_MODE`` — sender attached a
    #   fee but the recipient's ``communication_policy.mode`` would
    #   route the message to the inbox or reject it outright. Locking
    #   the fee in those cases would either let the recipient pocket
    #   the funds without the manifest UX (open mode = no ack step) or
    #   waste the lock entirely (closed mode). We refuse loudly so
    #   the sender knows why their funds were not locked.
    # * ``ATTENTION_FEE_LOCK_FAILED`` — backend escrow rejected the
    #   lock (insufficient sender balance, idempotency collision,
    #   wallet missing). Caller's next step: top up wallet / retry
    #   later.
    # * ``ATTENTION_FEE_NOT_LOCKED`` — caller hit the ack endpoint on
    #   a manifest entry that was never locked (sender did not attach
    #   a fee). Caller's next step: just call
    #   ``GET /communication/content`` instead — there is no fee to
    #   release. Distinct from "already acked" so the recipient SDK
    #   can suppress the ack call entirely on these entries.
    # * ``ATTENTION_FEE_ALREADY_ACKED`` — replay/double-ack on an
    #   already-released fee. Caller's next step: this is idempotent-
    #   safe; treat as success on the recipient side.
    # * ``ATTENTION_FEE_RELEASE_FAILED`` — backend escrow rejected
    #   the release (escrow missing, refunded out from under us,
    #   service down). 4xx so the SDK retries; the manifest entry's
    #   ``acked_at`` is left unset so the recipient can retry.
    ATTENTION_FEE_INVALID = "attention_fee_invalid"
    ATTENTION_FEE_REQUIRES_MANIFEST_MODE = "attention_fee_requires_manifest_mode"
    # ``content_url`` supplied but recipient policy routes to inbox or
    # rejection. The full ``message`` payload would be stored on ACN in
    # those modes, defeating the self-hosted contract. Return loudly so
    # the sender knows ACN did *not* skip payload storage.
    CONTENT_URL_REQUIRES_MANIFEST_MODE = "content_url_requires_manifest_mode"
    CONTENT_URL_BLOCKED = "content_url_blocked"
    ATTENTION_FEE_LOCK_FAILED = "attention_fee_lock_failed"
    ATTENTION_FEE_NOT_LOCKED = "attention_fee_not_locked"
    ATTENTION_FEE_ALREADY_ACKED = "attention_fee_already_acked"
    ATTENTION_FEE_RELEASE_FAILED = "attention_fee_release_failed"

    # ===== Onchain / ERC-8004 routes (sprint row #7) =====
    # All six codes are NEW in sprint #7 and are *route-local* — they
    # only surface from ``acn/routes/onchain.py``. The cross-module
    # ``AGENT_NOT_FOUND`` (×4 sites) and ``API_KEY_AGENT_MISMATCH``
    # (×1 site) are reused from the pilot / cross-module groups; they
    # do not get a new declaration here.
    #
    # ----- Why six distinct codes (vs a single ``ERC8004_FAILURE``
    # with ``details.kind``)? -----
    #
    # Each of the six is a *materially different* failure mode that
    # an SDK consumer wants to branch on without inspecting
    # ``details``:
    # * ``ERC8004_TOKEN_ID_MISSING`` — the agent has never bound a
    #   token. Caller's next step: prompt the user to call
    #   ``POST /agents/{id}/bind`` first. (Reachable from the
    #   reputation / validation read paths via the
    #   ``_parse_token_id_or_422`` helper.)
    # * ``ERC8004_TOKEN_ID_CORRUPT`` — the agent's stored
    #   ``erc8004_agent_id`` is non-numeric. Caller's next step: open
    #   a support ticket (this is operator-side data corruption, not
    #   user-actionable). Distinct from "missing" because the bind
    #   record exists but is malformed.
    # * ``ERC8004_CHAIN_MISMATCH`` — the bind request named a
    #   ``chain`` value that disagrees with ACN's configured chain.
    #   Caller's next step: omit the field or pass the matching
    #   server-derived value.
    # * ``ERC8004_TOKEN_ALREADY_BOUND`` — the requested
    #   ``token_id`` is already bound to a *different* agent. (Note:
    #   re-binding the same token to the same agent is idempotent
    #   and does not raise — this code only surfaces on cross-agent
    #   collisions.) Caller's next step: surface the 409 to the
    #   user and let them either pick a different token or contact
    #   the bound agent's owner.
    # * ``ERC8004_REGISTRATION_MISMATCH`` — the on-chain ``tokenURI``
    #   does not match ACN's expected agent-registration URL.
    #   Caller's next step: re-register on-chain with the correct
    #   ``agentURI`` and retry.
    # * ``ERC8004_NOT_BOUND`` — the agent exists but has no ERC-8004
    #   token binding (so reputation / validation queries can't
    #   proceed). Distinct from ``ERC8004_TOKEN_ID_MISSING`` because
    #   *this* code is raised at the *route entry point* (before the
    #   parse-or-422 helper is reached), and SDK clients want to
    #   render a friendlier "this agent has not bound an on-chain
    #   identity yet" message rather than the operator-tier
    #   ``token_id_missing`` diagnostic. Both 404, both essentially
    #   "no token to query against", but differentiated by *which
    #   layer* surfaced the absence.
    #
    # ----- Why six in *this* group, not lifted to ``cross-module``? -----
    #
    # None of these failure modes are expected to surface from any
    # other route module. ERC-8004 binding is unique to the
    # ``/api/v1/onchain/*`` namespace; if a future route ever needs
    # to surface "this token is corrupt", the right move is to lift
    # the code into the cross-module group at that point — not to
    # over-anticipate now.
    ERC8004_TOKEN_ID_MISSING = "erc8004_token_id_missing"
    ERC8004_TOKEN_ID_CORRUPT = "erc8004_token_id_corrupt"
    ERC8004_CHAIN_MISMATCH = "erc8004_chain_mismatch"
    ERC8004_TOKEN_ALREADY_BOUND = "erc8004_token_already_bound"
    ERC8004_REGISTRATION_MISMATCH = "erc8004_registration_mismatch"
    ERC8004_NOT_BOUND = "erc8004_not_bound"

    # ===== Payments routes (sprint row #5) =====
    # ``AGENT_NOT_FOUND`` (×2), ``API_KEY_AGENT_MISMATCH`` (×4), and
    # ``FROM_AGENT_MISMATCH`` (×1 — body.from_agent vs auth-key
    # mismatch) are reused from the pilot / cross-module groups; the
    # four codes below are payments-specific resource-existence
    # failures. ``INSUFFICIENT_BALANCE`` (in the reserved group below)
    # is intentionally NOT raised by ``payments.py`` today: balance
    # failures live one layer deeper (wallet / billing subsystem) and
    # don't surface at the route boundary in the current architecture.
    PAYMENT_CAPABILITY_NOT_FOUND = "payment_capability_not_found"
    PAYMENT_TASK_NOT_FOUND = "payment_task_not_found"
    TOKEN_PRICING_NOT_CONFIGURED = "token_pricing_not_configured"
    BILLING_TRANSACTION_NOT_FOUND = "billing_transaction_not_found"
    # Store-settlement bridge (ADR-0009 P1-A). Dedicated codes (not the
    # generic capability/task-not-found above) because they carry a store
    # ``order_id`` rather than an ``agent_id``/``task_id``, and a distinct
    # ``details`` shape per the cross-module schema contract.
    STORE_SETTLEMENT_SELLER_NOT_PAYABLE = "store_settlement_seller_not_payable"
    STORE_SETTLEMENT_NOT_FOUND = "store_settlement_not_found"

    # ===== Cross-module auth/permission/validation (sprint row #2b) =====
    # Shared by ``registry``, ``subnets``, and ``tasks`` routes.
    # Single-source-of-truth set so that SDK clients see consistent
    # error semantics for the same kind of caller error regardless of
    # which module emitted it. See ``docs/features/acn-error-schema.md``
    # section "Cross-module catalog (sprint row #2b)" for the
    # per-module raise-site matrix.
    AUTHENTICATION_REQUIRED = "authentication_required"
    INTERNAL_TOKEN_INVALID = "internal_token_invalid"
    MISSING_PERMISSION = "missing_permission"
    OWNERSHIP_MISMATCH = "ownership_mismatch"
    NOT_SUBNET_MEMBER = "not_subnet_member"
    INVALID_REQUEST = "invalid_request"
    VALIDATION_FAILED = "validation_failed"

    # ===== 5xx fallback (sanitised handler chain) =====
    INTERNAL_SERVER_ERROR = "internal_server_error"

    # ===== Reserved (declared, not yet raised by any route) =====
    # Rate-limit code — slowapi's ``_rate_limit_exceeded_handler``
    # currently owns 429 responses; this code is reserved for when
    # rate limit emission converges with the flat schema.
    WALLET_RATE_LIMIT_EXCEEDED = "wallet_rate_limit_exceeded"
    # Per-resource codes — picked up as each module flips ⏳ → ✅
    # in section 4 of ``docs/features/acn-error-schema.md``.
    INSUFFICIENT_BALANCE = "insufficient_balance"
    RESOURCE_CONFLICT = "resource_conflict"
    AGENT_HAS_OWNED_SUBNETS = "agent_has_owned_subnets"


_DEFAULT_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.AGENT_NOT_FOUND: "The requested agent could not be found.",
    ErrorCode.API_KEY_AGENT_MISMATCH: (
        "The API key does not match the agent identified by the request path."
    ),
    ErrorCode.FROM_AGENT_MISMATCH: (
        "The authenticated agent does not match the from_agent field in the request body."
    ),
    ErrorCode.AUTHENTICATION_REQUIRED: (
        "Authentication credentials were not provided for this protected endpoint."
    ),
    ErrorCode.INTERNAL_TOKEN_INVALID: (
        "The supplied internal token is invalid or the server is misconfigured."
    ),
    ErrorCode.COMMUNICATION_REJECTED: (
        "The recipient's communication policy denied this sender."
    ),
    ErrorCode.UNKNOWN_STRATEGY: (
        "The provided broadcast strategy is not recognised."
    ),
    ErrorCode.WALLET_RATE_LIMIT_EXCEEDED: (
        "The per-wallet message rate limit has been exceeded."
    ),
    ErrorCode.INTERNAL_SERVER_ERROR: (
        "An internal error occurred. Please try again later."
    ),
    ErrorCode.SUBNET_NOT_FOUND: "The requested subnet could not be found.",
    ErrorCode.TASK_NOT_FOUND: "The requested task could not be found.",
    ErrorCode.FOLLOW_LIMIT_EXCEEDED: (
        "The follower has reached the per-agent follow ceiling. "
        "Unfollow some agents first."
    ),
    ErrorCode.SELF_FOLLOW_FORBIDDEN: "An agent cannot follow itself.",
    ErrorCode.MANIFEST_ENTRY_NOT_FOUND: (
        "The requested manifest entry could not be found."
    ),
    ErrorCode.INBOX_MESSAGE_NOT_FOUND: (
        "The requested inbox message could not be found. "
        "It may have already been acknowledged or the route_id is incorrect."
    ),
    ErrorCode.SESSION_NOT_FOUND: (
        "The requested session could not be found or has already expired."
    ),
    ErrorCode.SESSION_ALREADY_ACCEPTED: (
        "This session invitation has already been accepted."
    ),
    ErrorCode.SESSION_EXPIRED: (
        "The session invitation has expired. The inviter must send a new invitation."
    ),
    ErrorCode.SESSION_FORBIDDEN: (
        "You are not a participant in this session."
    ),
    ErrorCode.MANIFEST_CONTENT_NOT_FOUND: (
        "The requested manifest content could not be found or has expired."
    ),
    ErrorCode.ATTENTION_FEE_INVALID: (
        "The supplied attention_fee value is invalid. Provide a positive "
        "integer amount within the allowed range and a supported currency."
    ),
    ErrorCode.ATTENTION_FEE_REQUIRES_MANIFEST_MODE: (
        "attention_fee is only honoured when the recipient is in manifest "
        "or allowlist mode. Drop the fee or wait for the recipient to "
        "switch modes."
    ),
    ErrorCode.CONTENT_URL_REQUIRES_MANIFEST_MODE: (
        "content_url is only valid when the recipient is in manifest mode. "
        "In inbox/open mode the full message payload is stored on ACN, "
        "defeating the self-hosted contract. Drop content_url or wait for "
        "the recipient to switch to manifest mode."
    ),
    ErrorCode.CONTENT_URL_BLOCKED: (
        "The provided content_url is not allowed. ACN only accepts https:// URLs "
        "pointing to public hostnames. Private IPs, loopback addresses, and "
        "non-HTTPS schemes are rejected to prevent SSRF attacks."
    ),
    ErrorCode.ATTENTION_FEE_LOCK_FAILED: (
        "The attention_fee could not be locked in escrow. Check the "
        "sender wallet balance and retry."
    ),
    ErrorCode.ATTENTION_FEE_NOT_LOCKED: (
        "The manifest entry has no attention_fee attached; nothing to "
        "release. Use GET /communication/content instead."
    ),
    ErrorCode.ATTENTION_FEE_ALREADY_ACKED: (
        "The attention_fee for this manifest entry has already been "
        "released. Subsequent ack calls are no-ops."
    ),
    ErrorCode.ATTENTION_FEE_RELEASE_FAILED: (
        "The attention_fee could not be released from escrow. Retry the "
        "ack call after a short backoff."
    ),
    ErrorCode.ERC8004_TOKEN_ID_MISSING: (
        "Agent has no ERC-8004 token ID."
    ),
    ErrorCode.ERC8004_TOKEN_ID_CORRUPT: (
        "Agent's stored ERC-8004 token ID is not a valid integer."
    ),
    ErrorCode.ERC8004_CHAIN_MISMATCH: (
        "The supplied chain identifier does not match the chain ACN is "
        "configured for."
    ),
    ErrorCode.ERC8004_TOKEN_ALREADY_BOUND: (
        "The requested ERC-8004 token ID is already bound to a different agent."
    ),
    ErrorCode.ERC8004_REGISTRATION_MISMATCH: (
        "The on-chain tokenURI does not match the expected agent-registration URL."
    ),
    ErrorCode.ERC8004_NOT_BOUND: (
        "Agent has not bound an ERC-8004 token ID yet."
    ),
    ErrorCode.PAYMENT_CAPABILITY_NOT_FOUND: (
        "No payment capability is registered for this agent."
    ),
    ErrorCode.PAYMENT_TASK_NOT_FOUND: "The requested payment task could not be found.",
    ErrorCode.TOKEN_PRICING_NOT_CONFIGURED: (
        "Token-based pricing is not configured for this agent."
    ),
    ErrorCode.BILLING_TRANSACTION_NOT_FOUND: (
        "The requested billing transaction could not be found."
    ),
    ErrorCode.STORE_SETTLEMENT_SELLER_NOT_PAYABLE: (
        "The seller agent does not accept platform-credit payments, so the "
        "store order cannot be mirrored as a payment task."
    ),
    ErrorCode.STORE_SETTLEMENT_NOT_FOUND: (
        "No payment task is recorded for the given store order id."
    ),
    ErrorCode.ALLOWLIST_CAPACITY_EXCEEDED: (
        "The owner's allowlist is at capacity. Remove some entries first."
    ),
    ErrorCode.SELF_ALLOWLIST_FORBIDDEN: (
        "An owner cannot add itself to its own allowlist."
    ),
    ErrorCode.MISSING_PERMISSION: (
        "The authenticated caller lacks the required permission for this operation."
    ),
    ErrorCode.OWNERSHIP_MISMATCH: (
        "The authenticated caller does not own the requested resource."
    ),
    ErrorCode.NOT_SUBNET_MEMBER: (
        "The authenticated caller is not a member of the required subnet."
    ),
    ErrorCode.INVALID_REQUEST: (
        "The request contains invalid data."
    ),
    ErrorCode.VALIDATION_FAILED: (
        "Request validation failed."
    ),
    ErrorCode.INSUFFICIENT_BALANCE: (
        "The requested operation cannot be completed due to insufficient balance."
    ),
    ErrorCode.RESOURCE_CONFLICT: (
        "The request conflicts with the current state of the resource."
    ),
    ErrorCode.AGENT_HAS_OWNED_SUBNETS: (
        "The agent cannot be deleted because it still owns one or more subnets. "
        "Transfer ownership or delete the subnets first."
    ),
    # ===== ADR-0004 Slice 2.3 — subnet admission =====
    ErrorCode.SUBNET_NOT_OWNER: (
        "The authenticated agent is not the owner of this subnet."
    ),
    ErrorCode.NOT_INVITEE: (
        "The authenticated agent is not the invitee of this invitation."
    ),
    ErrorCode.ALREADY_MEMBER: (
        "The agent is already a member of this subnet."
    ),
    ErrorCode.ALREADY_ON_ALLOWLIST: (
        "The agent is already on this subnet's allowlist."
    ),
    ErrorCode.JOIN_REQUEST_NOT_FOUND: (
        "The requested join request could not be found."
    ),
    ErrorCode.JOIN_REQUEST_PENDING: (
        "A pending join request for this (subnet, agent) pair already exists."
    ),
    ErrorCode.JOIN_REQUEST_ALREADY_DECIDED: (
        "This join request has already been decided and cannot be modified."
    ),
    ErrorCode.INVITATION_NOT_FOUND: (
        "The requested invitation could not be found."
    ),
    ErrorCode.INVITATION_PENDING: (
        "A pending invitation for this (subnet, agent) pair already exists."
    ),
    ErrorCode.INVITATION_ALREADY_DECIDED: (
        "This invitation has already been decided and cannot be modified."
    ),
    ErrorCode.INVALID_KIND_FILTER: (
        "The supplied kind filter is not valid for this endpoint. "
        "Invitations are queryable through the /invitations endpoint only."
    ),
}


class ACNErrorResponse(BaseModel):
    """Canonical flat ACN error response body.

    Both 4xx (emitted by ``ACNHTTPError`` handler) and 5xx (emitted by
    the sanitised ``_http_exception_handler``) responses share this
    shape during and after the deprecation window. During the 30-day
    transition the 5xx body additionally includes a legacy ``error``
    field (equal in value to ``error_code``) — the deprecation ticket
    in BACKLOG tracks its removal.
    """

    error_code: str = Field(
        ...,
        description="Stable ASCII error code (snake_case). The only field SDK clients should branch on.",
    )
    message: str = Field(
        ...,
        description="Human-readable prose describing the error. SDK clients MUST NOT string-match on this field.",
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Code-specific structured context. Field semantics depend on error_code; undocumented fields are not stable.",
    )
    request_id: str = Field(
        ...,
        description="Per-request UUID for support correlation. Echoed in the X-Request-ID response header.",
    )


_MIN_4XX_STATUS = 400
_MAX_4XX_STATUS = 500


class ACNHTTPError(Exception):
    """ACN-flavoured HTTP error carrying a stable ``error_code`` contract.

    Raised by route handlers when a *caller-actionable* 4xx response
    is appropriate. The central exception handler installed in
    ``acn.api`` translates an ``ACNHTTPError`` into a flat JSON body
    matching ``ACNErrorResponse``.

    .. note::
        ``ACNHTTPError`` is intentionally **not** a subclass of
        ``HTTPException``. The Starlette / FastAPI HTTPException
        handler in ``acn/api.py`` would otherwise also match it,
        creating an order-of-registration footgun. Keeping the two
        exception trees disjoint means each handler unambiguously
        owns one shape.

    Parameters
    ----------
    code:
        The stable ``ErrorCode`` clients should branch on.
    status_code:
        HTTP status code. **Must be in ``[400, 500)``** — 5xx responses
        are sanitised by a separate handler chain to prevent internal
        information disclosure, and allowing 5xx here would let a
        route author bypass that sanitisation by passing internal
        details in ``message`` or ``details``.
    message:
        Optional human-readable prose. Defaults to
        ``_DEFAULT_MESSAGES[code]`` when omitted, so simple call sites
        don't have to repeat boilerplate.
    details:
        Optional code-specific structured context. Keep keys stable
        per code and document them in
        ``docs/features/acn-error-schema.md``.
    headers:
        Optional response headers to merge into the JSON response
        (e.g. ``Retry-After``). The central handler unconditionally
        overrides ``X-Request-ID`` so caller-supplied values for that
        header are ignored.
    """

    def __init__(
        self,
        code: ErrorCode,
        status_code: int,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if not _MIN_4XX_STATUS <= status_code < _MAX_4XX_STATUS:
            raise ValueError(
                f"ACNHTTPError must use a 4xx status code (got {status_code}). "
                "5xx responses are sanitised by the central 5xx handler — "
                "raise HTTPException(500, str(e)) for those paths so the "
                "internal exception detail does not leak to anonymous callers."
            )
        self.code = code
        self.status_code = status_code
        self.message = message if message is not None else _DEFAULT_MESSAGES[code]
        self.details = details if details is not None else {}
        self.headers = headers
        super().__init__(self.message)


# =============================================================================
# OpenAPI ``responses=`` default (P3 schema-visibility ticket)
# =============================================================================
#
# Single-source-of-truth default ``responses`` mapping that every ACN
# router which raises ``ACNHTTPError`` should pass to ``APIRouter(...,
# responses=ACN_DEFAULT_RESPONSES)``. FastAPI cannot statically infer
# which routes raise ``ACNHTTPError`` (the exception type is opaque to
# the OpenAPI generator), so without this advertisement SDK type-gen
# consumers see ``HTTPValidationError`` / generic ``dict`` for 4xx
# bodies instead of the canonical ``ACNErrorResponse`` flat shape.
#
# Granularity choice (router-level default, NOT per-endpoint)
# ----------------------------------------------------------
# The trade-off was discussed in the P3 ticket; the conclusion was
# that per-endpoint precision (listing only the status codes that a
# specific endpoint actually raises) carries near-zero practical
# benefit for SDK consumers — generated client code branches on the
# response *body* (``error_code``) and HTTP status, not on which set
# of status codes a single endpoint *might* return. The maintenance
# cost, however, is high: every new ``ACNHTTPError`` raise site needs
# a matching decorator update, with drift risk on every refactor.
# Router-level default eliminates the drift, costs zero ongoing
# attention, and over-specifies a few unused status codes per
# endpoint — pure spec noise, never a correctness issue.
#
# 422 is intentionally NOT in this map: FastAPI auto-emits 422 with
# its own ``HTTPValidationError`` schema for pydantic body / query /
# path validation failures, and that schema is not (yet) aligned with
# ``ACNErrorResponse``. A separate P3 ticket tracks the alignment.
#
# 5xx codes are also intentionally absent: the central
# ``_http_exception_handler`` and ``_unhandled_exception_handler`` in
# ``acn/api.py`` emit a 5xx body that *also* matches
# ``ACNErrorResponse`` shape (during the deprecation window the body
# additionally carries a legacy ``error`` field), but advertising 5xx
# in this map is misleading: 5xx are sanitised, opaque, and not
# branched on by SDK clients the same way 4xx are.
ACN_DEFAULT_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {
        "model": ACNErrorResponse,
        "description": "Bad request — invalid input or constraint violation.",
    },
    401: {
        "model": ACNErrorResponse,
        "description": "Authentication required or credentials invalid.",
    },
    403: {
        "model": ACNErrorResponse,
        "description": "Permission denied — caller is authenticated but not authorised.",
    },
    404: {
        "model": ACNErrorResponse,
        "description": "Resource not found.",
    },
    409: {
        "model": ACNErrorResponse,
        "description": "Conflict with the current state of the resource.",
    },
    429: {
        "model": ACNErrorResponse,
        "description": "Rate limited — caller exceeded the per-bucket budget.",
    },
}


__all__ = [
    "ACN_DEFAULT_RESPONSES",
    "ACNErrorResponse",
    "ACNHTTPError",
    "ErrorCode",
    "_DEFAULT_MESSAGES",
]
