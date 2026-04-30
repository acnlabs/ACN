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
