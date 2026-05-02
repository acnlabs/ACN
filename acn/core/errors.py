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
    ErrorCode.INSUFFICIENT_BALANCE: (
        "The requested operation cannot be completed due to insufficient balance."
    ),
    ErrorCode.RESOURCE_CONFLICT: (
        "The request conflicts with the current state of the resource."
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
