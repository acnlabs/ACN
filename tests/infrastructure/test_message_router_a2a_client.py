"""Tests for MessageRouter's a2a-sdk 1.x client bridge."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from acn.infrastructure.messaging import message_router as router_module
from acn.infrastructure.messaging.message_router import MessageRouter, create_text_message


@pytest.mark.asyncio
async def test_router_uses_registered_endpoint_as_direct_legacy_jsonrpc_target(monkeypatch):
    """Registered endpoints are direct A2A JSON-RPC URLs, not card discovery roots."""
    requests: list[httpx.Request] = []
    real_async_client = httpx.AsyncClient

    async def _safe_resolve_target(_endpoint: str, *, allow_loopback: bool = False) -> None:
        return None

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        assert payload["method"] == "message/send"
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "kind": "message",
                    "messageId": "reply-1",
                    "role": "agent",
                    "parts": [{"kind": "text", "text": "ok"}],
                },
            },
        )

    def _async_client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(router_module, "safe_resolve_target", _safe_resolve_target)
    monkeypatch.setattr(router_module.httpx, "AsyncClient", _async_client_factory)

    agent_info = MagicMock()
    agent_info.endpoint = "https://agent.example.com/a2a"
    agent_info.communication_policy = {"mode": "open"}

    agent_service = AsyncMock()
    agent_service.find_agent = AsyncMock(return_value=agent_info)
    agent_service.is_alive = AsyncMock(return_value=True)

    router = MessageRouter(
        agent_service=agent_service,
        redis_client=AsyncMock(),
    )

    try:
        response = await router.route(
            from_agent="agent-a",
            to_agent="agent-b",
            message=create_text_message("hello"),
        )
    finally:
        await router.close()

    assert response.result.message_id == "reply-1"
    assert [str(request.url) for request in requests] == ["https://agent.example.com/a2a"]
