"""Implicit-heartbeat contract tests for the agent-auth dependencies.

Background — the implicit-heartbeat feature schedules a fire-and-forget
``AgentService.touch_alive(agent_id)`` after every authenticated
agent-API-key request so a busy agent never has to call
``POST /agents/{id}/heartbeat`` explicitly to stay ``online``. The renewal
itself is exercised in ``tests/services/test_agent_service.py``; this
module locks down the **other half** of the contract: the four dependency
entry points must actually wire the renewal into FastAPI's
``BackgroundTasks`` queue, and the *non-agent* branches (internal-token
infra calls) must explicitly NOT.

We test the dependency functions directly (no TestClient / no router) so
the assertion is "BackgroundTasks.add_task was called with the right
target & args" — the strongest signal that the production code path will
also enqueue the renewal. Each test mirrors one branch in
``acn/routes/dependencies.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks

from acn.routes.dependencies import (
    _schedule_alive_renewal,
    verify_agent_api_key,
    verify_owner_or_internal,
    verify_proxy_caller,
)


def _make_request() -> SimpleNamespace:
    """Minimal stand-in for ``fastapi.Request``.

    The dependencies only read ``.headers`` (for the rate-limit auth-failure
    audit path, which we never hit on success) and write ``.state.*``, so a
    SimpleNamespace with a SimpleNamespace ``state`` is sufficient.
    """
    return SimpleNamespace(
        state=SimpleNamespace(),
        headers={},
        client=SimpleNamespace(host="127.0.0.1"),
    )


def _agent_service_resolving(agent_id: str, api_key: str = "owner-key") -> AsyncMock:
    """AsyncMock that resolves ``api_key`` → an agent with ``agent_id``.

    ``touch_alive`` is left as the default AsyncMock attribute — the
    background task layer will call it (in real FastAPI) but we are
    only asserting that it was *scheduled*, not awaited, so the default
    AsyncMock is fine.
    """
    agent = SimpleNamespace(agent_id=agent_id, name="Test", wallet_address=None)

    async def _by_api_key(k: str):
        return agent if k == api_key else None

    svc = AsyncMock()
    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)
    return svc


@pytest.mark.asyncio
async def test_verify_agent_api_key_schedules_implicit_heartbeat():
    """Direct routes (``Authorization: Bearer acn_…``) must enqueue
    ``touch_alive`` for the resolved agent — this is the most common
    traffic shape (registry/communication/subnet REST calls)."""
    svc = _agent_service_resolving("agent-direct")
    bg = BackgroundTasks()
    request = _make_request()

    info = await verify_agent_api_key(
        request=request,
        background_tasks=bg,
        authorization="Bearer owner-key",
        agent_service=svc,
    )

    assert info["agent_id"] == "agent-direct"
    assert len(bg.tasks) == 1
    task = bg.tasks[0]
    assert task.func is svc.touch_alive
    assert task.args == ("agent-direct",)


@pytest.mark.asyncio
async def test_verify_proxy_caller_schedules_implicit_heartbeat():
    """Proxy routes (``X-ACN-Authorization: Bearer acn_…``) must also
    enqueue ``touch_alive`` for the *caller* (the downstream agent gets
    its own renewal when it handles its own next request)."""
    svc = _agent_service_resolving("agent-caller")
    bg = BackgroundTasks()
    request = _make_request()

    info = await verify_proxy_caller(
        request=request,
        background_tasks=bg,
        x_acn_authorization="Bearer owner-key",
        agent_service=svc,
    )

    assert info["agent_id"] == "agent-caller"
    assert len(bg.tasks) == 1
    assert bg.tasks[0].func is svc.touch_alive
    assert bg.tasks[0].args == ("agent-caller",)


@pytest.mark.asyncio
async def test_verify_owner_or_internal_agent_branch_schedules_renewal():
    """Owner-via-API-key branch is agent-driven traffic → must renew."""
    svc = _agent_service_resolving("agent-owner")
    bg = BackgroundTasks()
    request = _make_request()

    info = await verify_owner_or_internal(
        request=request,
        agent_id="agent-owner",
        background_tasks=bg,
        authorization="Bearer owner-key",
        x_internal_token=None,
        agent_service=svc,
    )

    assert info == {"caller_kind": "agent", "agent_id": "agent-owner"}
    assert len(bg.tasks) == 1
    assert bg.tasks[0].func is svc.touch_alive
    assert bg.tasks[0].args == ("agent-owner",)


@pytest.mark.asyncio
async def test_verify_owner_or_internal_internal_token_does_not_renew(monkeypatch):
    """Internal-token path is platform infrastructure (backend services,
    ops tooling), NOT an agent producing business traffic. It must
    explicitly NOT extend any agent's alive TTL, or every backend cron
    that calls into ACN would silently keep stale agents "online"
    forever — defeating the offline-detection guarantee."""
    from acn.routes import dependencies as deps

    # Stub the settings the internal-token branch reads. constant_time
    # compare requires a string with non-zero length.
    monkeypatch.setattr(
        deps.settings,
        "internal_api_token",
        "test-internal-token-min-32-chars-padding",
    )

    svc = AsyncMock()  # never resolved — get_agent_by_api_key must not be called
    bg = BackgroundTasks()
    request = _make_request()

    info = await verify_owner_or_internal(
        request=request,
        agent_id="agent-irrelevant",
        background_tasks=bg,
        authorization=None,
        x_internal_token="test-internal-token-min-32-chars-padding",
        agent_service=svc,
    )

    assert info == {"caller_kind": "internal"}
    assert bg.tasks == []
    svc.get_agent_by_api_key.assert_not_called()
    svc.touch_alive.assert_not_called()


def test_schedule_alive_renewal_none_background_tasks_is_noop():
    """Unit-test safety valve: callers that construct a dependency
    directly without a BackgroundTasks (legacy tests, ad-hoc scripts)
    must get a clean no-op rather than an ``AttributeError`` — the
    helper is the single point we can enforce that."""
    svc = AsyncMock()
    # Must not raise.
    _schedule_alive_renewal(None, svc, "agent-x")
    svc.touch_alive.assert_not_called()
