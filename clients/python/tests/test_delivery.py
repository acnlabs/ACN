"""Python SDK tests for ``get_delivery`` / ``set_delivery`` (ADR-0012)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from acn_client.client import ACNClient


def _make_client_with_stub(request_mock: AsyncMock) -> ACNClient:
    client = ACNClient(base_url="http://acn.test")
    client._request = request_mock  # type: ignore[method-assign]
    return client


@pytest.mark.asyncio
async def test_get_delivery_returns_server_payload():
    server_payload: dict[str, Any] = {
        "agent_id": "agent-1",
        "delivery": "direct",
        "endpoint": "https://agent.example.com/a2a",
        "communication_mode": "open",
    }
    request_mock = AsyncMock(return_value=server_payload)
    client = _make_client_with_stub(request_mock)

    result = await client.get_delivery("agent-1")

    assert result is server_payload
    request_mock.assert_awaited_once_with(
        "GET", "/api/v1/agents/agent-1/delivery"
    )


@pytest.mark.asyncio
async def test_set_delivery_relay():
    server_payload: dict[str, Any] = {
        "agent_id": "agent-1",
        "delivery": "relay",
        "endpoint": None,
        "communication_mode": "open",
        "next_step_hint": "run acn listen",
    }
    request_mock = AsyncMock(return_value=server_payload)
    client = _make_client_with_stub(request_mock)

    result = await client.set_delivery("agent-1", "relay")

    assert result["delivery"] == "relay"
    request_mock.assert_awaited_once_with(
        "PATCH",
        "/api/v1/agents/agent-1/delivery",
        json={"delivery": "relay"},
    )


@pytest.mark.asyncio
async def test_set_delivery_direct_with_endpoint():
    server_payload: dict[str, Any] = {
        "agent_id": "agent-1",
        "delivery": "direct",
        "endpoint": "https://agent.example.com/a2a",
        "communication_mode": "open",
        "a2a_handshake_ok": True,
    }
    request_mock = AsyncMock(return_value=server_payload)
    client = _make_client_with_stub(request_mock)

    result = await client.set_delivery(
        "agent-1", "direct", endpoint="https://agent.example.com/a2a"
    )

    assert result["delivery"] == "direct"
    request_mock.assert_awaited_once_with(
        "PATCH",
        "/api/v1/agents/agent-1/delivery",
        json={
            "delivery": "direct",
            "endpoint": "https://agent.example.com/a2a",
        },
    )


@pytest.mark.asyncio
async def test_set_delivery_direct_requires_endpoint():
    client = _make_client_with_stub(AsyncMock())
    with pytest.raises(ValueError, match="requires endpoint"):
        await client.set_delivery("agent-1", "direct")


@pytest.mark.asyncio
async def test_set_delivery_relay_rejects_endpoint():
    client = _make_client_with_stub(AsyncMock())
    with pytest.raises(ValueError, match="mutually exclusive"):
        await client.set_delivery(
            "agent-1", "relay", endpoint="https://agent.example.com/a2a"
        )


@pytest.mark.asyncio
async def test_set_delivery_rejects_unknown_transport():
    client = _make_client_with_stub(AsyncMock())
    with pytest.raises(ValueError, match="direct' or 'relay"):
        await client.set_delivery("agent-1", "none")
