"""Tests for ``AuditLogger.query_public_broadcast_events`` filtering contract."""

from __future__ import annotations

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
