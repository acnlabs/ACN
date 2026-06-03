"""Tests for ADR-0012 Mode B proxy adapter ``_relay_or_inbox``.

The proxy entry (`api.acnlabs.dev/api/v1/agents/{id}`) stands in for an
agent's ``real_endpoint``. For an agent that registered without a public
HTTP endpoint, ``_proxy_to_agent`` delegates to ``_relay_or_inbox``:

* live WS control channel  → relay in real time, return the agent's reply
* connected but slow        → 504
* offline + root A2A POST    → park in inbox, 202
* offline + any other verb   → 503 (nothing meaningful to queue)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from acn.routes.registry import _relay_or_inbox


def _make_request(body: bytes = b'{"jsonrpc":"2.0"}') -> MagicMock:
    request = MagicMock()
    request.body = AsyncMock(return_value=body)
    request.headers = {}
    return request


def _caller() -> dict:
    return {"agent_id": "agent-sender", "name": "Sender"}


@pytest.mark.asyncio
async def test_relay_hit_returns_agent_response():
    ws_manager = MagicMock()
    ws_manager.relay_request_to_agent = AsyncMock(
        return_value={
            "status": 200,
            "headers": {"content-type": "application/json"},
            "body": '{"result":"ok"}',
        }
    )
    with patch("acn.routes.dependencies.get_ws_manager", return_value=ws_manager):
        resp = await _relay_or_inbox(
            request=_make_request(),
            agent_id="agent-target",
            method="POST",
            rest_path="",
            caller=_caller(),
        )
    assert resp.status_code == 200
    assert resp.body == b'{"result":"ok"}'
    ws_manager.relay_request_to_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_relay_timeout_returns_504():
    ws_manager = MagicMock()
    ws_manager.relay_request_to_agent = AsyncMock(side_effect=TimeoutError())
    with patch("acn.routes.dependencies.get_ws_manager", return_value=ws_manager):
        with pytest.raises(HTTPException) as exc_info:
            await _relay_or_inbox(
                request=_make_request(),
                agent_id="agent-target",
                method="POST",
                rest_path="",
                caller=_caller(),
            )
    assert exc_info.value.status_code == 504


@pytest.mark.asyncio
async def test_offline_root_post_parks_in_inbox_202():
    ws_manager = MagicMock()
    ws_manager.relay_request_to_agent = AsyncMock(return_value=None)  # offline
    router = MagicMock()
    router._store_inbox = AsyncMock()
    with (
        patch("acn.routes.dependencies.get_ws_manager", return_value=ws_manager),
        patch("acn.routes.dependencies.get_router", return_value=router),
    ):
        resp = await _relay_or_inbox(
            request=_make_request(),
            agent_id="agent-target",
            method="POST",
            rest_path="",
            caller=_caller(),
        )
    assert resp.status_code == 202
    payload = json.loads(resp.body)
    assert payload["delivery_mode"] == "inbox"
    router._store_inbox.assert_awaited_once()
    kwargs = router._store_inbox.await_args.kwargs
    assert kwargs["to_agent"] == "agent-target"
    assert kwargs["log_entry"]["from_agent"] == "agent-sender"


@pytest.mark.asyncio
async def test_offline_non_post_returns_503():
    ws_manager = MagicMock()
    ws_manager.relay_request_to_agent = AsyncMock(return_value=None)  # offline
    with patch("acn.routes.dependencies.get_ws_manager", return_value=ws_manager):
        with pytest.raises(HTTPException) as exc_info:
            await _relay_or_inbox(
                request=_make_request(),
                agent_id="agent-target",
                method="PUT",
                rest_path="",
                caller=_caller(),
            )
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_offline_subpath_post_returns_503():
    # A sub-path POST is not a root A2A message; offline it has nowhere
    # meaningful to queue, so it must 503 rather than silently inbox.
    ws_manager = MagicMock()
    ws_manager.relay_request_to_agent = AsyncMock(return_value=None)
    with patch("acn.routes.dependencies.get_ws_manager", return_value=ws_manager):
        with pytest.raises(HTTPException) as exc_info:
            await _relay_or_inbox(
                request=_make_request(),
                agent_id="agent-target",
                method="POST",
                rest_path="some/sub/path",
                caller=_caller(),
            )
    assert exc_info.value.status_code == 503
