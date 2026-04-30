"""Manifest Dispatcher — Phase 2 PR #1 review fix (P0-A1).

Single-source helper that turns an inbound A2A ``Message`` into a
manifest-queue entry, pushes the WS notification, and bumps the
divert metric. Both ``MessageRouter.route()`` (HTTP / A2A / DLQ
paths) and ``SubnetManager.forward_request()`` (WebSocket
push path) call into this helper when ``PolicyDecision.route_to ==
"manifest"`` so manifest-mode semantics are uniform across all
inbound paths.

Why a separate module rather than inline helpers on the router:

* The PR #1 review caught that subnet routing was bypassing manifest
  mode entirely (subnet_manager only called
  ``check_inbound_or_raise``, which returns silently for manifest
  mode → message went straight through WS). Centralising the
  divert logic here is the structural fix — anyone who runs a
  policy gate ahead of message delivery now has one obvious thing
  to call.

* Keeping the dispatcher in ``infrastructure/messaging`` (alongside
  ``MessageRouter`` and ``SubnetManager``) preserves the
  dependency direction: services don't depend on infrastructure.
  The dispatcher itself depends on ``ManifestService`` (services)
  and the WebSocket / metrics interfaces (infrastructure /
  monitoring) — both downward-pointing.

* Metric emission is centralised here too. ``messages_diverted_to_manifest_total``
  is the manifest-side companion to
  ``messages_rejected_by_policy_total``: both count
  "policy-shaped traffic that didn't reach the inbox", split by
  cause. Putting the inc inside the dispatcher follows the Phase 1
  rule "count at the closest layer" — every manifest divert,
  whether from router or subnet_manager, lands here exactly once.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from a2a.types import Message  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from ...services.manifest_service import ManifestEntry, ManifestService
    from .websocket_manager import WebSocketManager

logger = logging.getLogger(__name__)

# Cap on how much of the inbound A2A message body we scan when
# constructing the manifest summary. Mirrors ``MAX_SUMMARY_LEN`` on
# the storage side; extracting at the same cap saves an intermediate
# copy if the original message is large.
_MANIFEST_SUMMARY_LEN = 200

# Empty / structured-only fallback string. Surfaced when neither a
# TextPart nor a DataPart yields anything useful so the recipient
# always sees a non-blank summary in their manifest UI.
_EMPTY_SUMMARY_PLACEHOLDER = "[empty message]"


def extract_summary(message: Message) -> str:
    """Derive a short human-readable preview from an A2A ``Message``.

    Walks the ``parts`` list in order and concatenates the contents
    of every ``TextPart``. ``DataPart`` (structured payloads) is
    summarised as ``[data: N keys]`` so a manifest entry built from
    a pure-data message — common on agent-to-agent function-call
    style traffic — still shows *something* in the listing instead
    of an empty row that gives the recipient no signal.

    The function is exported (no leading underscore) so the
    router-layer test suite can pin its behaviour without touching
    private symbols.
    """
    chunks: list[str] = []
    parts = getattr(message, "parts", None) or []
    for part in parts:
        # A2A wraps every concrete part in a ``Part(root=...)``
        # discriminated union; duck-typing through ``.root`` works
        # for both wrapped and unwrapped parts.
        inner = getattr(part, "root", None) or part
        text = getattr(inner, "text", None)
        if isinstance(text, str) and text:
            chunks.append(text)
        else:
            data = getattr(inner, "data", None)
            if isinstance(data, dict):
                key_count = len(data)
                chunks.append(
                    f"[data: {key_count} key{'s' if key_count != 1 else ''}]"
                )
        if sum(len(c) for c in chunks) >= _MANIFEST_SUMMARY_LEN:
            break
    summary = " ".join(chunks).strip()
    if not summary:
        summary = _EMPTY_SUMMARY_PLACEHOLDER
    return summary[:_MANIFEST_SUMMARY_LEN]


class ManifestDispatcher:
    """Coordinator for manifest-mode message divert.

    Holds the three collaborators a divert needs (storage, WS push,
    metric counter) so callers — ``MessageRouter`` and
    ``SubnetManager`` — only need a reference to one object.

    Args:
        manifest_service: Required. Without it the dispatcher has
            nowhere to persist; the constructor would have failed
            anyway when callers tried to invoke ``dispatch``.
        ws_manager: Optional. When ``None`` the dispatcher still
            persists the manifest entry but skips the realtime
            ``manifest_notification`` push. Recipients pick up the
            entry on next ``GET /communication/manifest/{agent_id}``
            poll. Defaulting to ``None`` lets test fixtures and
            messaging-only legacy harnesses construct the
            dispatcher without standing up WebSocket plumbing.
        metrics: Optional. When ``None`` the divert metric is
            silently skipped. Same rationale as above; production
            wiring (acn/api.py) installs the real metrics
            collector.

    Note that the dispatcher does NOT take a policy_service
    reference — the gate decision is the *caller's* responsibility.
    By the time we're inside ``dispatch`` the divert is already a
    foregone conclusion (``decision.route_to == "manifest"``).
    """

    def __init__(
        self,
        manifest_service: ManifestService,
        *,
        ws_manager: WebSocketManager | None = None,
        metrics: Any = None,
    ) -> None:
        self.manifest_service = manifest_service
        self.ws_manager = ws_manager
        self.metrics = metrics

    async def dispatch(
        self,
        *,
        owner_id: str,
        sender_id: str,
        message: Message,
        path: str,
        route_id: str | None = None,
    ) -> ManifestEntry:
        """Persist the message to the manifest queue + push WS + count.

        Args:
            owner_id: Recipient agent id (the manifest queue tenant).
            sender_id: Sender agent id (or ``system:<slug>`` for ACN
                internal callers, though the system bypass usually
                short-circuits this branch upstream).
            message: A2A ``Message`` carrying the original payload.
                ``model_dump()`` is used to JSON-encode the body for
                the content store; the summary is derived via
                ``extract_summary``.
            path: Caller tag — ``"router"`` for the HTTP / A2A /
                DLQ paths, ``"subnet"`` for the subnet WebSocket
                push path. Surfaced as a metric label so operators
                can correlate divert volume with ingress channel
                (e.g. spotting "all manifest traffic comes via
                subnet").
            route_id: Optional correlation id for log lines. The
                router supplies its own 8-char id; the subnet
                manager can pass the request_id. ``None`` falls
                back to the entry's mid in log output.

        Returns:
            The persisted ``ManifestEntry``. Callers use ``.mid`` /
            ``.ts_ms`` for their own response shape.
        """
        content_dict = (
            message.model_dump() if hasattr(message, "model_dump") else {"raw": str(message)}
        )
        summary = extract_summary(message)

        entry = await self.manifest_service.write(
            owner_id=owner_id,
            sender_id=sender_id,
            summary=summary,
            content=content_dict,
        )

        # Best-effort WS push. The recipient still gets the manifest
        # entry on next list call even if the push is lost (pubsub
        # partition, recipient not connected, etc.). We do NOT
        # rollback the manifest write on push failure — losing a
        # notification is better than losing the message itself.
        if self.ws_manager is not None:
            try:
                await self.ws_manager.send_to_user(
                    user_id=owner_id,
                    message={
                        "type": "manifest_notification",
                        "mid": entry.mid,
                        "sender_id": sender_id,
                        "summary": entry.summary,
                        "ts": entry.ts_ms,
                        "content_size": entry.content_size,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "manifest_ws_push_failed mid=%s owner=%s error=%s",
                    entry.mid,
                    owner_id,
                    exc,
                )

        # P1-B3: divert counter, partner of messages_rejected_by_policy_total.
        # Wrapped in try/except because the metrics collector may go
        # through Redis; we don't want a metric-side outage to roll
        # back a successful manifest write.
        if self.metrics is not None:
            try:
                await self.metrics.inc_counter(
                    "messages_diverted_to_manifest_total",
                    labels={"path": path},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "manifest_metric_failed mid=%s error=%s",
                    entry.mid,
                    exc,
                )

        rid = route_id or entry.mid
        logger.info(
            "[%s] manifest divert: %s -> %s mid=%s path=%s",
            rid,
            sender_id,
            owner_id,
            entry.mid,
            path,
        )
        return entry
