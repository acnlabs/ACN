"""Unit tests — ManifestDispatcher.

Phase 2 PR #1 review fix (P0-A1): the dispatcher is the
single-source helper for manifest divert. Both the router and the
subnet manager call into it. This file pins:

- The two collaborators (manifest_service, ws_manager) are invoked
  in the right order with the right shape.
- The metric counter fires with ``path`` label set by the caller
  (router / subnet) — required for the
  ``messages_diverted_to_manifest_total`` dashboard split.
- WS push and metric inc are best-effort: failures log, do NOT roll
  back the manifest write, and do NOT raise out of ``dispatch``.
- ``ws_manager=None`` and ``metrics=None`` work — used by lean test
  fixtures and the messaging-only legacy harness.
- ``extract_summary`` honours TextPart, DataPart, and the empty
  fallback placeholder (P1-B1 review fix).

The high-level "what gets stored where" is covered in
``test_manifest_service.py``; here the focus is the dispatcher's
orchestration contract.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from a2a.types import (  # type: ignore[import-untyped]
    DataPart,
    Message,
    Part,
    Role,
    TextPart,
)

from acn.infrastructure.messaging.manifest_dispatcher import (
    ManifestDispatcher,
    extract_summary,
)
from acn.services.manifest_service import ManifestEntry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_manifest_service() -> MagicMock:
    """Mock ManifestService that returns a deterministic entry."""
    service = MagicMock()
    service.write = AsyncMock(
        return_value=ManifestEntry(
            mid="cafebabecafebabecafebabecafebabe",
            sender_id="agent-a",
            summary="ignored — dispatcher passes through what we store",
            ts_ms=1714377600000,
            content_size=128,
        )
    )
    return service


@pytest.fixture
def stub_ws_manager() -> MagicMock:
    ws = MagicMock()
    ws.send_to_user = AsyncMock()
    return ws


@pytest.fixture
def stub_metrics() -> MagicMock:
    metrics = MagicMock()
    metrics.inc_counter = AsyncMock()
    return metrics


def _make_text_message(text: str) -> Message:
    return Message(
        role=Role.user,
        message_id="msg-1",
        parts=[Part(root=TextPart(text=text))],
    )


# ---------------------------------------------------------------------------
# 1. Happy path: store + WS + metric in the right shape
# ---------------------------------------------------------------------------


class TestDispatchHappyPath:
    @pytest.mark.asyncio
    async def test_writes_to_manifest_service_with_summary(
        self, stub_manifest_service, stub_ws_manager, stub_metrics
    ):
        dispatcher = ManifestDispatcher(
            manifest_service=stub_manifest_service,
            ws_manager=stub_ws_manager,
            metrics=stub_metrics,
        )
        message = _make_text_message("hello world")

        entry = await dispatcher.dispatch(
            owner_id="agent-b",
            sender_id="agent-a",
            message=message,
            path="router",
        )

        stub_manifest_service.write.assert_awaited_once()
        kwargs = stub_manifest_service.write.await_args.kwargs
        assert kwargs["owner_id"] == "agent-b"
        assert kwargs["sender_id"] == "agent-a"
        assert kwargs["summary"] == "hello world"
        # Content is the model-dumped message dict, not raw bytes.
        assert isinstance(kwargs["content"], dict)
        assert kwargs["content"]["role"] == "user"
        assert entry.mid == "cafebabecafebabecafebabecafebabe"

    @pytest.mark.asyncio
    async def test_pushes_ws_notification_with_expected_shape(
        self, stub_manifest_service, stub_ws_manager, stub_metrics
    ):
        """The WS push payload is part of the realtime SDK contract.

        Frontend / agent clients listen for
        ``type=manifest_notification`` and read summary/mid/ts to
        decide whether to pull the body. Drift on these field names
        breaks every connected client silently.
        """
        dispatcher = ManifestDispatcher(
            manifest_service=stub_manifest_service,
            ws_manager=stub_ws_manager,
            metrics=stub_metrics,
        )

        await dispatcher.dispatch(
            owner_id="agent-b",
            sender_id="agent-a",
            message=_make_text_message("hi"),
            path="router",
        )

        stub_ws_manager.send_to_user.assert_awaited_once()
        push_kwargs = stub_ws_manager.send_to_user.await_args.kwargs
        assert push_kwargs["user_id"] == "agent-b"
        payload = push_kwargs["message"]
        assert payload["type"] == "manifest_notification"
        assert payload["mid"] == "cafebabecafebabecafebabecafebabe"
        assert payload["sender_id"] == "agent-a"
        assert payload["ts"] == 1714377600000
        assert payload["content_size"] == 128

    @pytest.mark.asyncio
    async def test_increments_divert_counter_with_path_label(
        self, stub_manifest_service, stub_ws_manager, stub_metrics
    ):
        """The ``path`` label is the ingress channel separator.

        Operators rely on ``messages_diverted_to_manifest_total{path=router|subnet}``
        to spot one path silently bypassing manifest mode (the
        original PR #1 bug). Drift here would re-merge the channels
        in dashboards.
        """
        dispatcher = ManifestDispatcher(
            manifest_service=stub_manifest_service,
            ws_manager=stub_ws_manager,
            metrics=stub_metrics,
        )

        await dispatcher.dispatch(
            owner_id="agent-b",
            sender_id="agent-a",
            message=_make_text_message("hi"),
            path="subnet",
        )

        stub_metrics.inc_counter.assert_awaited_once_with(
            "messages_diverted_to_manifest_total",
            labels={"path": "subnet"},
        )


# ---------------------------------------------------------------------------
# 2. Best-effort isolation: WS / metric failures must not roll back
# ---------------------------------------------------------------------------


class TestBestEffortIsolation:
    @pytest.mark.asyncio
    async def test_ws_push_failure_does_not_raise(
        self, stub_manifest_service, stub_metrics
    ):
        """Lost notification is acceptable; lost manifest entry is not.

        The recipient picks up the entry on next list call even if
        the realtime push fails. Raising out of ``dispatch`` would
        force the caller to handle a partially-completed divert
        (manifest written but caller sees error) — much worse UX
        than a silently dropped notification.
        """
        ws = MagicMock()
        ws.send_to_user = AsyncMock(side_effect=ConnectionError("ws gone"))
        dispatcher = ManifestDispatcher(
            manifest_service=stub_manifest_service,
            ws_manager=ws,
            metrics=stub_metrics,
        )

        # No raise.
        entry = await dispatcher.dispatch(
            owner_id="agent-b",
            sender_id="agent-a",
            message=_make_text_message("hi"),
            path="router",
        )

        assert entry.mid == "cafebabecafebabecafebabecafebabe"
        # The manifest write still happened.
        stub_manifest_service.write.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_metric_failure_does_not_raise(
        self, stub_manifest_service, stub_ws_manager
    ):
        """Same isolation rule applies to the metric inc.

        A Redis-side metric counter failure must NOT roll back the
        manifest write. The trace count being slightly off is much
        better than the recipient losing a message because metrics
        had a bad day.
        """
        metrics = MagicMock()
        metrics.inc_counter = AsyncMock(side_effect=ConnectionError("redis flake"))
        dispatcher = ManifestDispatcher(
            manifest_service=stub_manifest_service,
            ws_manager=stub_ws_manager,
            metrics=metrics,
        )

        entry = await dispatcher.dispatch(
            owner_id="agent-b",
            sender_id="agent-a",
            message=_make_text_message("hi"),
            path="router",
        )

        assert entry.mid == "cafebabecafebabecafebabecafebabe"


# ---------------------------------------------------------------------------
# 3. Optional collaborators: None just means "skip that step"
# ---------------------------------------------------------------------------


class TestOptionalCollaborators:
    @pytest.mark.asyncio
    async def test_no_ws_manager_skips_push_silently(
        self, stub_manifest_service, stub_metrics
    ):
        """Lean fixtures construct the dispatcher without WS plumbing.

        Used by services-level tests that want to exercise divert
        write semantics without standing up the WebSocket subsystem.
        """
        dispatcher = ManifestDispatcher(
            manifest_service=stub_manifest_service,
            ws_manager=None,
            metrics=stub_metrics,
        )

        entry = await dispatcher.dispatch(
            owner_id="agent-b",
            sender_id="agent-a",
            message=_make_text_message("hi"),
            path="router",
        )
        assert entry.mid == "cafebabecafebabecafebabecafebabe"
        # Metric still fires — independent of WS.
        stub_metrics.inc_counter.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_metrics_skips_counter_silently(
        self, stub_manifest_service, stub_ws_manager
    ):
        dispatcher = ManifestDispatcher(
            manifest_service=stub_manifest_service,
            ws_manager=stub_ws_manager,
            metrics=None,
        )

        entry = await dispatcher.dispatch(
            owner_id="agent-b",
            sender_id="agent-a",
            message=_make_text_message("hi"),
            path="router",
        )
        assert entry.mid == "cafebabecafebabecafebabecafebabe"
        # WS still fires.
        stub_ws_manager.send_to_user.assert_awaited_once()


# ---------------------------------------------------------------------------
# 4. extract_summary — Text / Data / empty fallback (P1-B1 review fix)
# ---------------------------------------------------------------------------


class TestExtractSummary:
    def test_text_part_returned_verbatim(self):
        message = _make_text_message("check the report")
        assert extract_summary(message) == "check the report"

    def test_multiple_text_parts_joined(self):
        message = Message(
            role=Role.user,
            message_id="msg-1",
            parts=[
                Part(root=TextPart(text="hello")),
                Part(root=TextPart(text="world")),
            ],
        )
        assert extract_summary(message) == "hello world"

    def test_data_part_summarised_as_key_count(self):
        """DataPart shouldn't dump JSON into the summary.

        Raw JSON in a notification preview is ugly *and* a PII risk
        (sensitive payloads end up on the listing endpoint). The
        ``[data: N keys]`` placeholder is enough signal for the
        recipient to know "there's a structured payload here, fetch
        if interested".
        """
        message = Message(
            role=Role.user,
            message_id="msg-1",
            parts=[
                Part(
                    root=DataPart(
                        data={"order_id": "ord-1", "amount": 42, "currency": "USD"}
                    )
                )
            ],
        )
        assert extract_summary(message) == "[data: 3 keys]"

    def test_single_data_key_uses_singular(self):
        """Cosmetic but cheap to pin: ``1 key`` not ``1 keys``."""
        message = Message(
            role=Role.user,
            message_id="msg-1",
            parts=[Part(root=DataPart(data={"k": "v"}))],
        )
        assert extract_summary(message) == "[data: 1 key]"

    def test_text_and_data_mixed_text_takes_priority(self):
        """When both kinds are present, TextPart content leads.

        Recipients usually prefer human-readable preview text; the
        DataPart key count is a fallback, not the primary signal.
        """
        message = Message(
            role=Role.user,
            message_id="msg-1",
            parts=[
                Part(root=TextPart(text="meeting notes:")),
                Part(root=DataPart(data={"attendees": [], "duration": 30})),
            ],
        )
        summary = extract_summary(message)
        assert "meeting notes:" in summary

    def test_empty_message_falls_back_to_placeholder(self):
        """Never write a blank summary — defeats the purpose of the
        manifest listing UI. The fallback string lets the recipient
        at least see "something is here, sender_id is X"."""
        message = Message(
            role=Role.user,
            message_id="msg-1",
            parts=[],
        )
        assert extract_summary(message) == "[empty message]"

    def test_long_text_capped(self):
        """Cap is mostly defence-in-depth — manifest_service truncates
        again at write time. We still cap here to avoid building a
        large intermediate string for a 1MB inbound message body.
        """
        long_text = "x" * 10000
        message = Message(
            role=Role.user,
            message_id="msg-1",
            parts=[Part(root=TextPart(text=long_text))],
        )
        summary = extract_summary(message)
        assert len(summary) <= 200
