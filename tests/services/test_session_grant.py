"""An accepted session is a short-lived permit to send the full message.

A pending invitation must not open that permit. Closing the session
removes it.
"""

from __future__ import annotations

import pytest
from fakeredis import aioredis as fakeredis_async

from acn.services.session_service import SessionService


@pytest.fixture
async def sessions() -> SessionService:
    client = fakeredis_async.FakeRedis(decode_responses=True)
    return SessionService(client)


@pytest.mark.asyncio
async def test_pending_invite_is_not_a_grant(sessions: SessionService):
    await sessions.invite("agent-a", "agent-b", ttl_seconds=60)
    assert await sessions.has_active_grant("agent-a", "agent-b") is False
    assert await sessions.has_active_grant("agent-b", "agent-a") is False


@pytest.mark.asyncio
async def test_accept_opens_grant_both_ways_until_close(sessions: SessionService):
    entry = await sessions.invite("agent-a", "agent-b", ttl_seconds=60)
    accepted = await sessions.accept(entry.session_id, "agent-b")
    assert accepted is not None
    assert await sessions.has_active_grant("agent-a", "agent-b") is True
    assert await sessions.has_active_grant("agent-b", "agent-a") is True

    await sessions.close(entry.session_id, "agent-a")
    assert await sessions.has_active_grant("agent-a", "agent-b") is False


@pytest.mark.asyncio
async def test_closing_an_older_session_keeps_the_newer_grant(sessions: SessionService):
    older = await sessions.invite("agent-a", "agent-b", ttl_seconds=60)
    await sessions.accept(older.session_id, "agent-b")
    newer = await sessions.invite("agent-a", "agent-b", ttl_seconds=60)
    await sessions.accept(newer.session_id, "agent-b")

    await sessions.close(older.session_id, "agent-a")
    assert await sessions.has_active_grant("agent-a", "agent-b") is True

    await sessions.close(newer.session_id, "agent-b")
    assert await sessions.has_active_grant("agent-a", "agent-b") is False
