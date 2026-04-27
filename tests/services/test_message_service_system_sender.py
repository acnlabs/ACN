"""MessageService + internal ``system:`` sender (14.5-1 follow-up).

``POST /communication/internal/send`` passes ``from_agent`` in the reserved
``system:<slug>`` namespace. Those IDs are not registered rows — the HTTP
layer validates the namespace; ``send_message`` must not require a DB row
for the sender or internal channel always 404s with
``Sender agent system:... not found``.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from a2a.types import Message, TextPart

from acn.services.message_service import MessageService


def _make_msg() -> Message:
    return Message(
        message_id=str(uuid.uuid4()),
        role="user",
        parts=[TextPart(text="smoke")],
    )


def _make_svc(*, recipient_online: bool = False) -> tuple:
    """Return (svc, repo, router) with a pre-wired healthy recipient."""
    recipient = MagicMock()
    recipient.is_online = MagicMock(return_value=recipient_online)
    recipient.status = MagicMock(value="online" if recipient_online else "offline")

    async def find_by_id(aid: str):
        return recipient if aid == "recv-uuid" else None

    repo = MagicMock()
    repo.find_by_id = AsyncMock(side_effect=find_by_id)

    router_mock = MagicMock()
    router_mock.route = AsyncMock(return_value={"message_id": "m1", "status": "queued"})

    return MessageService(router_mock, repo), repo, router_mock


@pytest.mark.asyncio
async def test_send_message_skips_sender_db_lookup_for_system_namespace():
    """``system:`` sender must NOT require a registry row."""
    svc, repo, router_mock = _make_svc()

    out = await svc.send_message("system:agentplanet-backend", "recv-uuid", _make_msg())

    assert out["message_id"] == "m1"
    # Only recipient lookup — never ``find_by_id("system:agentplanet-backend")``.
    ids_queried = [c[0][0] for c in repo.find_by_id.call_args_list]
    assert ids_queried == ["recv-uuid"]
    router_mock.route.assert_awaited_once()


@pytest.mark.asyncio
async def test_priority_kwarg_not_forwarded_to_router():
    """``priority`` must be stripped before reaching ``MessageRouter.route``
    (the router's signature does not accept it — passing via **kwargs causes
    TypeError in production)."""
    svc, _repo, router_mock = _make_svc()

    await svc.send_message(
        "system:agentplanet-backend", "recv-uuid", _make_msg(), priority="high"
    )

    # Verify route() was called without 'priority'.
    _, route_kwargs = router_mock.route.call_args
    assert "priority" not in route_kwargs, "priority leaked into router.route()"


@pytest.mark.asyncio
async def test_regular_sender_still_requires_registry_row():
    """Non-system senders must still be validated against the agent registry."""
    from acn.core.exceptions import AgentNotFoundException

    recipient = MagicMock()
    recipient.is_online = MagicMock(return_value=True)
    recipient.status = MagicMock(value="online")

    async def find_by_id(aid: str):
        return recipient if aid == "recv-uuid" else None  # sender "user-1" → None

    repo = MagicMock()
    repo.find_by_id = AsyncMock(side_effect=find_by_id)
    router_mock = MagicMock()
    router_mock.route = AsyncMock(return_value={})
    svc = MessageService(router_mock, repo)

    with pytest.raises(AgentNotFoundException):
        await svc.send_message("user-1", "recv-uuid", _make_msg())
