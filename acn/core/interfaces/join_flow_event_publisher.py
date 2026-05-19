"""Join-flow event publisher contract (ADR-0004 Phase 2 Slice 2.2).

ADR-0004 §"Webhook event catalogue" introduces eight new event
types that fire on every state transition of the three-in-one
``subnet_join_requests`` table. The actual webhook payload, HMAC
signing, retry envelope, etc. live in the existing
``acn/protocols/ap2/webhook.py::WebhookService`` and are wired in
Slice 2.4.

Slice 2.2 — this slice — implements the domain logic
(``JoinFlowService`` + the eight new ``SubnetService`` methods)
**without** taking a hard dependency on ``WebhookService``. Doing
so would smear two concerns:

1. Slice 2.2 would have to add eight new entries to
   ``WebhookEventType`` (an enum that lives in the AP2 protocol
   module and whose membership is itself an ADR-0004 §Org Harness
   impact decision Slice 2.4 owns).
2. Tests would need to stub the heavy ``WebhookService`` (Redis +
   httpx + retry loop) for every code path that emits an event,
   even though Slice 2.2's only interest in the event is "did we
   call publish() with the right arguments".

This module solves both problems with a tiny adapter:

* :class:`JoinFlowEventType` — Slice 2.2-owned enum carrying the
  eight string event names. The values are the canonical
  ``"subnet.join_*"`` / ``"subnet.invitation_*"`` strings from
  ADR §Webhook event catalogue, so Slice 2.4's mapping to
  ``WebhookEventType`` is a 1-1 string lookup.

* :class:`IJoinFlowEventPublisher` — the abstract port the
  service layer depends on. Single ``publish()`` method that
  takes the canonical ADR-0004 payload tuple plus the
  ``trigger`` / ``via`` discriminators from §"Merge-path event
  mapping". Slice 2.2 ships :class:`NoOpJoinFlowEventPublisher`
  (see ``acn/services/_no_op_join_flow_event_publisher.py``)
  bound by ``api.py``; Slice 2.4 ships a concrete
  ``WebhookJoinFlowEventPublisher`` that adapts the call into
  ``WebhookService.send_to``.

The asymmetric ``trigger`` / ``via`` defaults match ADR
§"Payload shape": ``trigger='explicit'`` for direct API actions
and ``via=None`` for the non-merge branches.
"""

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Literal

from ..entities import Subnet, SubnetJoinRequest

JoinFlowEventTrigger = Literal["explicit", "auto_on_join", "auto_on_invite"]
"""Identifies *why* a transition fired. See ADR §"Payload shape".

* ``explicit`` — direct API action (owner clicked approve, applicant
  clicked withdraw, etc.). The vast majority of events.
* ``auto_on_join`` — the agent's ``join`` call triggered an
  auto-resolution of a pending invitation (branches 3 / 4 in
  §join).
* ``auto_on_invite`` — the owner's ``invite`` call triggered an
  auto-approval of a pending join_request (the symmetric merge
  path in §"POST /invitations" "Merge path").
"""

JoinFlowEventVia = Literal["self_join", "owner_invite", "allowlist"]
"""Identifies *which side* initiated the collision on a merge path.

Populated only when ``trigger != 'explicit'``. ``None`` on every
direct-action event. See ADR §"Payload shape" for the contract.
"""


class JoinFlowEventType(StrEnum):
    """Canonical event names for ADR-0004 join-flow lifecycle events.

    The string values match the ADR §"Webhook event catalogue"
    table verbatim. Slice 2.4 will mirror these into
    :class:`acn.protocols.ap2.webhook.WebhookEventType` via a
    string-equality lookup, so any drift between the two enums
    surfaces as a wiring-time test failure rather than a runtime
    mystery.

    Allowlist add / remove deliberately have no event types here —
    ADR §"Webhook event catalogue" notes "Allowlist configuration
    changes (add / remove) do NOT emit webhooks; the allowlist is
    configuration state, not lifecycle."
    """

    JOIN_REQUESTED = "subnet.join_requested"
    JOIN_APPROVED = "subnet.join_approved"
    JOIN_REJECTED = "subnet.join_rejected"
    JOIN_WITHDRAWN = "subnet.join_withdrawn"

    INVITATION_SENT = "subnet.invitation_sent"
    INVITATION_ACCEPTED = "subnet.invitation_accepted"
    INVITATION_REJECTED = "subnet.invitation_rejected"
    INVITATION_CANCELED = "subnet.invitation_canceled"


class IJoinFlowEventPublisher(ABC):
    """Port the service layer uses to emit join-flow lifecycle events.

    A single ``publish()`` method intentionally covers all eight
    event types so Slice 2.2 can dispatch on the
    :class:`JoinFlowEventType` enum at the service-layer call site
    (one ``publish(JoinFlowEventType.JOIN_APPROVED, ...)`` per
    transition path) without growing eight separate method names.

    The implementation is expected to be best-effort — webhook
    delivery failures must not block the underlying state
    transition. ADR-0004 §Webhook reuses the ADR-0003 contract
    ("never break the lifecycle on webhook failure"); concrete
    implementations are responsible for catching and logging
    transport errors internally.
    """

    @abstractmethod
    async def publish(
        self,
        event: JoinFlowEventType,
        *,
        subnet: Subnet,
        request: SubnetJoinRequest,
        trigger: JoinFlowEventTrigger = "explicit",
        via: JoinFlowEventVia | None = None,
    ) -> None:
        """Emit a single join-flow lifecycle event.

        Args:
            event: One of the eight ``JoinFlowEventType`` members.
            subnet: The ``Subnet`` entity the event is scoped to.
                Implementations read ``harness_url`` /
                ``harness_secret`` / ``parent_subnet_id`` from here
                — Slice 2.2 callers must pass the freshly-fetched
                subnet so a webhook fired off a stale snapshot
                doesn't address a deleted Harness.
            request: The ``SubnetJoinRequest`` row whose lifecycle
                the event describes. Carries ``request_id``,
                ``agent_id``, ``kind``, ``initiated_by``,
                ``decided_by`` for the payload (see ADR §"Payload
                shape").
            trigger: ``"explicit"`` (default) for direct API
                actions; ``"auto_on_join"`` / ``"auto_on_invite"``
                on the three merge paths.
            via: Populated only on merge paths; ``None`` on direct
                actions. See ADR §"Merge-path event mapping" for
                the (trigger, via) table.

        Implementations MUST NOT raise on transport errors — the
        caller is in the middle of a successful state transition
        and the webhook is an audit signal, not part of the
        atomic write. The :class:`NoOpJoinFlowEventPublisher`
        Slice 2.2 ships honours this trivially.
        """
