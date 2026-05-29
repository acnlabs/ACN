"""Tests for ``AuditLogger.query_public_broadcast_events`` filtering contract."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from acn.monitoring.audit import AuditEvent, AuditEventType, AuditLevel, AuditLogger


def _event(
    eid: str,
    event_type: AuditEventType,
    *,
    eligible: object | None = None,
) -> AuditEvent:
    details = {}
    if eligible is not None:
        details["public_broadcast_eligible"] = eligible
    return AuditEvent(
        id=eid,
        timestamp=datetime.now(UTC),
        event_type=event_type,
        level=AuditLevel.INFO,
        target_id=f"target-{eid}",
        target_type="agent",
        details=details,
    )


@pytest.mark.asyncio
async def test_query_public_broadcast_events_filters_by_strict_boolean_true() -> None:
    logger = AuditLogger(redis=AsyncMock())
    logger.query_events = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            _event("e1", AuditEventType.AGENT_REGISTERED, eligible=True),
            _event("e2", AuditEventType.AGENT_REGISTERED, eligible=False),
            _event("e3", AuditEventType.AGENT_REGISTERED),  # missing key
            _event("e4", AuditEventType.AGENT_REGISTERED, eligible="true"),  # malformed
        ]
    )

    events = await logger.query_public_broadcast_events(limit=10)

    assert [e.id for e in events] == ["e1"]


@pytest.mark.asyncio
async def test_query_public_broadcast_events_honors_event_type_filter_and_pagination() -> None:
    logger = AuditLogger(redis=AsyncMock())
    logger.query_events = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            _event("e1", AuditEventType.AGENT_REGISTERED, eligible=True),
            _event("e2", AuditEventType.SUBNET_CREATED, eligible=True),
            _event("e3", AuditEventType.AGENT_REGISTERED, eligible=True),
        ]
    )

    events = await logger.query_public_broadcast_events(
        event_types=[AuditEventType.AGENT_REGISTERED],
        limit=1,
        offset=1,
    )

    assert [e.id for e in events] == ["e3"]


def test_to_public_broadcast_payload_for_agent_registered_is_whitelisted() -> None:
    event = _event("e1", AuditEventType.AGENT_REGISTERED, eligible=True)
    event.actor_id = "secret-actor"
    event.actor_type = "agent"
    event.source_ip = "203.0.113.7"
    event.user_agent = "sensitive-agent"
    event.details.update(
        {
            "source": "join",
            "visibility": "real",
            "public_broadcast_eligible": True,
            "internal_note": "must-not-leak",
        }
    )

    payload = AuditLogger.to_public_broadcast_payload(event)

    assert payload == {
        "schema_version": 1,
        "event_id": "e1",
        "timestamp": event.timestamp.isoformat(),
        "event_type": "agent_registered",
        "agent_id": "target-e1",
        "source": "join",
    }


def test_to_public_broadcast_payload_for_subnet_and_non_eligible_cases() -> None:
    subnet_event = _event("e2", AuditEventType.SUBNET_CREATED, eligible=True)
    subnet_event.target_id = "subnet-public-1"
    subnet_event.details["join_policy"] = "open"
    payload = AuditLogger.to_public_broadcast_payload(subnet_event)
    assert payload == {
        "schema_version": 1,
        "event_id": "e2",
        "timestamp": subnet_event.timestamp.isoformat(),
        "event_type": "subnet_created",
        "subnet_id": "subnet-public-1",
        "join_policy": "open",
    }

    hidden_event = _event("e3", AuditEventType.SUBNET_CREATED, eligible=False)
    assert AuditLogger.to_public_broadcast_payload(hidden_event) is None


@pytest.mark.asyncio
async def test_log_event_publishes_fixed_schema_to_public_ws_channel() -> None:
    redis = AsyncMock()
    logger = AuditLogger(redis=redis)
    event_id = await logger.log_event(
        event_type=AuditEventType.AGENT_REGISTERED,
        target_id="agent-realtime-1",
        target_type="agent",
        details={
            "source": "join",
            "public_broadcast_eligible": True,
            "internal_only": "drop-me",
        },
    )

    redis.publish.assert_awaited_once()
    channel, raw_message = redis.publish.await_args.args
    assert channel == "acn:ws:broadcast:system-events"
    message = json.loads(raw_message)
    assert message["type"] == "public_system_event"
    assert message["event"] == {
        "schema_version": 1,
        "event_id": event_id,
        "timestamp": message["event"]["timestamp"],
        "event_type": "agent_registered",
        "agent_id": "agent-realtime-1",
        "source": "join",
    }


@pytest.mark.asyncio
async def test_log_event_skips_public_ws_publish_for_non_eligible_event() -> None:
    redis = AsyncMock()
    logger = AuditLogger(redis=redis)
    await logger.log_event(
        event_type=AuditEventType.SUBNET_CREATED,
        target_id="subnet-private",
        target_type="subnet",
        details={"public_broadcast_eligible": False, "join_policy": "approval"},
    )

    redis.publish.assert_not_awaited()
