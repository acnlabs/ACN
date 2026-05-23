"""No-op join-flow event publisher (ADR-0004 Phase 2 Slice 2.2).

Slice 2.2 ships the domain logic for the eight new join-flow
lifecycle events but explicitly defers the webhook transport
(``WebhookService.send_to``) and the
:class:`acn.protocols.ap2.webhook.WebhookEventType` enum
extension to Slice 2.4. This stub keeps the service layer's
single-port contract intact in the meantime:
:class:`acn.services.subnet_service.SubnetService` and
:class:`acn.services.join_flow_service.JoinFlowService` always
hold a non-``None`` publisher reference, regardless of whether
Slice 2.4 is wired yet, so the call sites stay free of
"``if self._publisher is not None``" guards.

The stub logs every call at debug level so production logs
(``acn.services._no_op_join_flow_event_publisher``) clearly
distinguish "Slice 2.2 deployment, no webhook ever fired" from
"Slice 2.4 deployment but harness_url missing on this subnet"
once Slice 2.4 lands.
"""

import structlog  # type: ignore[import-untyped]

from ..core.entities import Subnet, SubnetJoinRequest
from ..core.interfaces.join_flow_event_publisher import (
    IJoinFlowEventPublisher,
    JoinFlowEventTrigger,
    JoinFlowEventType,
    JoinFlowEventVia,
)

logger = structlog.get_logger()


class NoOpJoinFlowEventPublisher(IJoinFlowEventPublisher):
    """:class:`IJoinFlowEventPublisher` no-op for Slice 2.2 deployments.

    Every ``publish()`` call short-circuits to a debug log. The
    log line carries the event type + subnet_id + request_id so
    operators auditing a Slice-2.2-only deployment can still
    reconstruct the join-flow timeline from log streams while
    Slice 2.4's real publisher is en route.
    """

    async def publish(
        self,
        event: JoinFlowEventType,
        *,
        subnet: Subnet,
        request: SubnetJoinRequest,
        trigger: JoinFlowEventTrigger = "explicit",
        via: JoinFlowEventVia | None = None,
    ) -> None:
        """Log-only stub. See class docstring."""
        # NB: structlog reserves the ``event`` keyword for the
        # log message itself; we name the join-flow event slot
        # ``join_flow_event`` here so the bound logger doesn't
        # collide on call.
        logger.debug(
            "join_flow_event_dropped_slice_2_2",
            join_flow_event=event.value,
            slug=subnet.slug,
            request_id=request.request_id,
            agent_id=request.agent_id,
            kind=request.kind,
            status=request.status,
            trigger=trigger,
            via=via,
        )
