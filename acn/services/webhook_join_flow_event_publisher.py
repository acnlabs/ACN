"""WebhookService-backed join-flow event publisher (ADR-0004 Slice 2.4).

Concrete :class:`IJoinFlowEventPublisher` implementation that adapts the
service-layer port into a real
:meth:`acn.protocols.ap2.webhook.WebhookService.send_to` call against
the subnet's registered Org Harness webhook.

Why an adapter rather than calling ``WebhookService`` from the service
layer directly: the service layer was authored in Slice 2.2 against a
narrow port (``IJoinFlowEventPublisher.publish``) precisely so it
would stay free of the AP2 protocol package — Slice 2.4 owns the
mapping into ``WebhookEventType`` and the payload shape decisions.
This file is that mapping.

Contracts pinned here:

1. **No harness → no-op success.** If ``subnet.harness_url`` is unset
   the publisher returns silently (matches ADR-0003's gate; the
   per-target ``WebhookService.send_to`` already short-circuits on
   empty URL, but checking upfront avoids the cost of building the
   payload).

2. **Transport failure does not block the lifecycle.** Any exception
   raised from ``WebhookService.send_to`` is caught and logged at
   error level. Callers (``JoinFlowService`` / ``SubnetService``) are
   in the middle of an already-committed state transition — re-raising
   here would either roll back the row (unacceptable per ADR-0004
   §"Cross-slice acceptance") or escape into the HTTP route layer as
   a 500 after the state machine already advanced.

3. **Enum mapping is 1-1.** Every
   :class:`JoinFlowEventType` member maps to exactly one
   :class:`WebhookEventType` member, looked up by string equality.
   New event types must update both enums; the no-drift contract is
   pinned by ``tests/services/test_join_flow_webhook_enum_mapping.py``.

4. **Payload shape matches ADR §"Payload shape".** The ``data`` block
   is the canonical
   ``{subnet_id, agent_id, request_id, parent_slug, kind,
   initiated_by, decided_by, trigger, via}``
   dict. ``task_id`` on the wrapping :class:`WebhookPayload` is set to
   the ``subnet_id`` (Harnesses key on subnet, not on a payment task);
   this matches the existing ADR-0003 convention used by
   ``do_join_subnet`` / ``do_leave_subnet``.
"""

from __future__ import annotations

import structlog  # type: ignore[import-untyped]

from ..core.entities import Subnet, SubnetJoinRequest
from ..core.interfaces.join_flow_event_publisher import (
    IJoinFlowEventPublisher,
    JoinFlowEventTrigger,
    JoinFlowEventType,
    JoinFlowEventVia,
)
from ..protocols.ap2.webhook import WebhookEventType, WebhookService

logger = structlog.get_logger()


# 1-1 mapping between the service-layer enum and the protocol enum.
# Both enums carry the same string values per ADR §"Webhook event
# catalogue"; the lookup is by membership rather than by string so a
# rename on either side surfaces as a static type error instead of a
# runtime KeyError. The companion no-drift test verifies the mapping
# stays exhaustive whenever either enum is touched.
_EVENT_MAP: dict[JoinFlowEventType, WebhookEventType] = {
    JoinFlowEventType.JOIN_REQUESTED: WebhookEventType.SUBNET_JOIN_REQUESTED,
    JoinFlowEventType.JOIN_APPROVED: WebhookEventType.SUBNET_JOIN_APPROVED,
    JoinFlowEventType.JOIN_REJECTED: WebhookEventType.SUBNET_JOIN_REJECTED,
    JoinFlowEventType.JOIN_WITHDRAWN: WebhookEventType.SUBNET_JOIN_WITHDRAWN,
    JoinFlowEventType.INVITATION_SENT: WebhookEventType.SUBNET_INVITATION_SENT,
    JoinFlowEventType.INVITATION_ACCEPTED: WebhookEventType.SUBNET_INVITATION_ACCEPTED,
    JoinFlowEventType.INVITATION_REJECTED: WebhookEventType.SUBNET_INVITATION_REJECTED,
    JoinFlowEventType.INVITATION_CANCELED: WebhookEventType.SUBNET_INVITATION_CANCELED,
}


class WebhookJoinFlowEventPublisher(IJoinFlowEventPublisher):
    """Adapts :class:`IJoinFlowEventPublisher.publish` to ``WebhookService``.

    Constructed once at composition root (``acn/api.py``) with the
    process-wide :class:`WebhookService` instance and bound onto both
    :class:`acn.services.subnet_service.SubnetService` and
    :class:`acn.services.join_flow_service.JoinFlowService` so all
    nine emit sites (seven in ``SubnetService``, two in
    ``JoinFlowService``) reach the real transport.
    """

    def __init__(self, webhook_service: WebhookService) -> None:
        self._webhook_service = webhook_service

    async def publish(
        self,
        event: JoinFlowEventType,
        *,
        subnet: Subnet,
        request: SubnetJoinRequest,
        trigger: JoinFlowEventTrigger = "explicit",
        via: JoinFlowEventVia | None = None,
    ) -> None:
        """Emit one join-flow event to the subnet's Org Harness.

        Returns silently on every failure mode (no harness URL, HTTP
        timeout, 5xx exhaustion, KeyError on unknown event). The
        caller is mid-transaction and cannot tolerate an exception
        bubbling up after the row has been committed.
        """
        # Gate 1: subnets without a registered Harness URL skip every
        # webhook regardless of event. Matches the existing ADR-0003
        # behaviour in ``do_join_subnet`` so operators only see
        # webhook traffic for subnets they explicitly opted in.
        if not subnet.harness_url:
            logger.debug(
                "join_flow_webhook_skipped_no_harness",
                join_flow_event=event.value,
                slug=subnet.slug,
                request_id=request.request_id,
            )
            return

        # Gate 2: defensive — an event added to JoinFlowEventType but
        # not yet mirrored into WebhookEventType would silently drop
        # the webhook. Log loud so observability catches it; the
        # no-drift test pins this at build time but we keep the
        # runtime guard for belt-and-braces (a brand-new event would
        # otherwise crash callers that depended on the prior shape).
        try:
            wire_event = _EVENT_MAP[event]
        except KeyError:
            logger.error(
                "join_flow_webhook_unmapped_event",
                join_flow_event=event.value,
                slug=subnet.slug,
                request_id=request.request_id,
            )
            return

        data = {
            "slug": subnet.slug,
            "agent_id": request.agent_id,
            "request_id": request.request_id,
            "parent_slug": subnet.parent_slug,
            "kind": request.kind,
            "initiated_by": request.initiated_by,
            "decided_by": request.decided_by,
            "trigger": trigger,
            "via": via,
        }

        try:
            await self._webhook_service.send_to(
                url=subnet.harness_url,
                secret=subnet.harness_secret,
                event=wire_event,
                # ADR-0003 convention — task_id on the wrapping
                # :class:`WebhookPayload` carries the subnet_id for
                # non-payment events. Harnesses key on ``data.slug``
                # directly so this is a transport-only field.
                task_id=subnet.slug,
                data=data,
                outbox=False,  # join-flow lifecycle: fire-and-forget, reconcile via GET /allowlist
            )
        except Exception as exc:  # noqa: BLE001 — see class docstring.
            logger.error(
                "join_flow_webhook_delivery_failed",
                join_flow_event=event.value,
                slug=subnet.slug,
                request_id=request.request_id,
                error=str(exc),
                exc_info=True,
            )
            # Swallow on purpose. ADR §"Cross-slice acceptance":
            # "Webhook delivery failures **do not** roll back the
            # underlying DB transaction" — the row is already
            # persisted; surfacing this exception would either
            # corrupt the transaction (we're past the commit) or
            # propagate a 500 to the user after a successful state
            # transition, both of which violate the lifecycle
            # invariant.
            return
