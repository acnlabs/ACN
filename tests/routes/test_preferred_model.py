"""POST /api/v1/agents/{id}/preferred-model — Host → listen default-model apply."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from acn.core.errors import ACNHTTPError
from acn.core.exceptions import AgentNotFoundException
from acn.routes.registry import (
    _PROXY_HOP_BY_HOP_HEADERS,
    PREFERRED_MODEL_APPLY_HEADER,
    _proxy_to_agent,
    _relay_or_inbox,
    relay_and_confirm_preferred_model,
)

_MODEL = "minimax/minimax-m2.5"


def _agent(preferred: str = _MODEL, endpoint: str | None = None) -> MagicMock:
    agent = MagicMock()
    agent.agent_id = "agent-target"
    agent.metadata = {"preferred_model": preferred}
    agent.endpoint = endpoint
    return agent


def _svc(*, preferred: str = _MODEL, endpoint: str | None = None) -> AsyncMock:
    svc = AsyncMock()
    agent = _agent(preferred, endpoint=endpoint)
    svc.get_agent = AsyncMock(return_value=agent)
    svc.set_desired_preferred_model = AsyncMock(return_value=agent)
    svc.clear_desired_preferred_model = AsyncMock(return_value=agent)
    return svc


def _ws(relayed):
    ws = MagicMock()
    ws.relay_request_to_agent = AsyncMock(return_value=relayed)
    return ws


@pytest.mark.asyncio
async def test_relay_confirms_from_heartbeat():
    ws = _ws(
        {
            "status": 200,
            "body": '{"ok":true,"preferred_model":"minimax/minimax-m2.5"}',
        }
    )
    svc = _svc()
    out = await relay_and_confirm_preferred_model(
        "agent-target", _MODEL, svc, ws_manager=ws
    )
    assert out["preferred_model"] == _MODEL
    call = ws.relay_request_to_agent.await_args
    assert call.kwargs["method"] == "POST"
    assert call.kwargs["path"] == "/acn/v1/preferred-model"
    assert call.kwargs["headers"]["x-acn-preferred-model-apply"] == "1"
    svc.set_desired_preferred_model.assert_awaited_once()
    svc.clear_desired_preferred_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_offline_returns_503():
    svc = _svc()
    with pytest.raises(HTTPException) as ei:
        await relay_and_confirm_preferred_model(
            "agent-target", _MODEL, svc, ws_manager=_ws(None)
        )
    assert ei.value.status_code == 503
    svc.clear_desired_preferred_model.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_returns_504():
    ws = MagicMock()
    ws.relay_request_to_agent = AsyncMock(side_effect=TimeoutError())
    svc = _svc()
    with pytest.raises(HTTPException) as ei:
        await relay_and_confirm_preferred_model(
            "agent-target", _MODEL, svc, ws_manager=ws
        )
    assert ei.value.status_code == 504
    svc.clear_desired_preferred_model.assert_awaited_once()


@pytest.mark.asyncio
async def test_listen_reject_returns_409():
    svc = _svc()
    ws = _ws({"status": 409, "body": '{"error":"hook_exit_1"}'})
    with pytest.raises(ACNHTTPError) as ei:
        await relay_and_confirm_preferred_model(
            "agent-target", _MODEL, svc, ws_manager=ws
        )
    assert ei.value.status_code == 409
    assert ei.value.details["reason"] == "preferred_model_apply_failed"
    assert ei.value.details["detail"] == "hook_exit_1"
    svc.clear_desired_preferred_model.assert_awaited_once()


@pytest.mark.asyncio
async def test_not_confirmed_returns_409():
    svc = _svc(preferred="old/model")
    ws = _ws({"status": 200, "body": '{"ok":true}'})
    with pytest.raises(ACNHTTPError) as ei:
        await relay_and_confirm_preferred_model(
            "agent-target",
            _MODEL,
            svc,
            ws_manager=ws,
        )
    assert ei.value.status_code == 409
    assert ei.value.details["reason"] == "preferred_model_not_confirmed"
    svc.clear_desired_preferred_model.assert_awaited_once()


@pytest.mark.asyncio
async def test_relay_body_cannot_confirm_without_heartbeat():
    """Listen 200 body is not enough — stored heartbeat preferred must match."""
    svc = _svc(preferred="old/model")
    ws = _ws({"status": 200, "body": '{"ok":true,"preferred_model":"minimax/minimax-m2.5"}'})
    with pytest.raises(ACNHTTPError) as ei:
        await relay_and_confirm_preferred_model(
            "agent-target", _MODEL, svc, ws_manager=ws
        )
    assert ei.value.status_code == 409
    assert ei.value.details["reason"] == "preferred_model_not_confirmed"
    svc.clear_desired_preferred_model.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_agent_after_relay():
    svc = _svc()
    svc.get_agent = AsyncMock(side_effect=AgentNotFoundException("agent-target"))
    ws = _ws({"status": 200, "body": '{"ok":true,"preferred_model":"minimax/minimax-m2.5"}'})
    with pytest.raises(ACNHTTPError) as ei:
        await relay_and_confirm_preferred_model(
            "agent-target", _MODEL, svc, ws_manager=ws
        )
    assert ei.value.status_code == 404
    svc.clear_desired_preferred_model.assert_awaited_once()


@pytest.mark.asyncio
async def test_uninitialized_ws_manager_is_offline():
    svc = _svc()
    with patch("acn.routes.dependencies.get_ws_manager", side_effect=RuntimeError("no ws")):
        with pytest.raises(HTTPException) as ei:
            await relay_and_confirm_preferred_model("agent-target", _MODEL, svc)
    assert ei.value.status_code == 503
    svc.clear_desired_preferred_model.assert_awaited_once()


@pytest.mark.asyncio
async def test_mode_a_without_listen_is_offline():
    """Public HTTP endpoint is not a Host-apply channel (no shared secret)."""
    svc = _svc(endpoint="https://agent.example/a2a")
    with pytest.raises(HTTPException) as ei:
        await relay_and_confirm_preferred_model(
            "agent-target", _MODEL, svc, ws_manager=_ws(None)
        )
    assert ei.value.status_code == 503
    svc.clear_desired_preferred_model.assert_awaited_once()


@pytest.mark.asyncio
async def test_unexpected_error_clears_desired():
    ws = MagicMock()
    ws.relay_request_to_agent = AsyncMock(side_effect=RuntimeError("boom"))
    svc = _svc()
    with pytest.raises(RuntimeError):
        await relay_and_confirm_preferred_model(
            "agent-target", _MODEL, svc, ws_manager=ws
        )
    svc.clear_desired_preferred_model.assert_awaited_once()


@pytest.mark.asyncio
async def test_proxy_rejects_preferred_model_path():
    svc = AsyncMock()
    svc.get_agent = AsyncMock(return_value=_agent())
    with pytest.raises(ACNHTTPError) as ei:
        await _proxy_to_agent(
            request=MagicMock(),
            agent_id="victim",
            method="POST",
            rest_path="acn/v1/preferred-model",
            agent_service=svc,
            caller={"agent_id": "attacker"},
        )
    assert ei.value.status_code == 403
    assert ei.value.details["reason"] == "preferred_model_owner_only"
    svc.get_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_proxy_preferred_model_unknown_agent_is_404():
    svc = AsyncMock()
    svc.get_agent = AsyncMock(side_effect=AgentNotFoundException("victim"))
    with pytest.raises(ACNHTTPError) as ei:
        await _proxy_to_agent(
            request=MagicMock(),
            agent_id="victim",
            method="POST",
            rest_path="acn/v1/preferred-model",
            agent_service=svc,
            caller={"agent_id": "attacker"},
        )
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_relay_or_inbox_rejects_preferred_model_path():
    with pytest.raises(ACNHTTPError) as ei:
        await _relay_or_inbox(
            request=MagicMock(),
            agent_id="victim",
            method="POST",
            rest_path="/acn/v1/preferred-model",
            caller={"agent_id": "attacker"},
        )
    assert ei.value.status_code == 403
    assert ei.value.details["reason"] == "preferred_model_owner_only"


def test_public_proxy_strips_owner_apply_marker():
    assert PREFERRED_MODEL_APPLY_HEADER in _PROXY_HOP_BY_HOP_HEADERS
