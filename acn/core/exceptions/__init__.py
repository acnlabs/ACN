"""Business Exceptions

Domain-specific exceptions.
"""


class ACNException(Exception):
    """Base exception for ACN"""

    pass


class AgentNotFoundException(ACNException):
    """Agent not found"""

    pass


class SubnetNotFoundException(ACNException):
    """Subnet not found"""

    pass


class PolicyRejected(ACNException):
    """Inbound message was rejected by the recipient's communication_policy.

    Carries a short ``reason`` code (used for HTTP error code mapping and
    ``message_rejected_by_policy_total{reason}`` metric labels) and an
    optional human-readable ``reject_reason`` provided by the recipient
    in their policy. ``recipient_id`` is preserved so audit / log
    handlers can attribute the rejection without re-querying the
    registry.

    Raised by ``PolicyCheckService.check_inbound_or_raise``; callers in
    ``MessageRouter.route`` and ``SubnetManager.forward_request``
    short-circuit on this exception (no inbox write, no DLQ).

    See docs/features/acn-communication-economic-model.md
    "Phase 1 网关执行点决策".
    """

    def __init__(
        self,
        reason: str,
        reject_reason: str | None = None,
        recipient_id: str | None = None,
    ) -> None:
        self.reason = reason
        self.reject_reason = reject_reason
        self.recipient_id = recipient_id
        message = f"{reason}: {reject_reason}" if reject_reason else reason
        super().__init__(message)


# ---------------------------------------------------------------------------
# Allowlist domain exceptions (Phase 2 PR #2)
# ---------------------------------------------------------------------------
#
# Hoisted from ``services/allowlist_service.py`` to ``core/exceptions`` so
# the Postgres repository can raise ``AllowlistCapacityExceededError``
# directly when the database-side capacity trigger fires (PR #2 v3 review
# P1-A1 fix — TOCTOU race resolved by a per-owner advisory lock + check
# inside a ``BEFORE INSERT`` trigger). Keeping the type in core/ avoids a
# repo→service import cycle while letting all three layers (repo, service,
# routes) reference the same canonical exception class.
class SelfAllowlistError(ACNException):
    """Owner attempted to add itself to its own allowlist.

    Mirrors the self-follow rule: the operation has no semantic meaning
    (sender == recipient already passes via the ``open`` short-circuit
    in ``PolicyCheckService.check_inbound``) and would clutter audit
    surfaces. Surfaced as 400 by the route layer.
    """


class AllowlistCapacityExceededError(ACNException):
    """Owner's allowlist already at ``MAX_ALLOWLIST_SIZE`` (=500).

    Raised by either the service-layer pre-flight check (cheap path)
    or the Postgres ``trg_agent_allowlist_capacity`` trigger (race-
    safe last line of defence). The trigger uses a per-owner
    ``pg_advisory_xact_lock`` to serialise concurrent INSERTs for the
    same owner — see migration ``f6a7b8c9d0e1`` for the full SQL.
    The route layer surfaces this as 429 with a "remove some entries
    first" hint.
    """
