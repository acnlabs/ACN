"""Tests for POST /api/v1/invoke (AgentRouter network door)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import AgentNotFoundException
from acn.routes.dependencies import (
    get_agent_service,
    get_audit,
    get_message_service,
    get_metrics,
    limiter,
)

VALID_INTERNAL_TOKEN = "test-internal-token-min-32-chars-padding"


@pytest.fixture
def stub_metrics():
    m = AsyncMock()
    m.inc_message_count = AsyncMock()
    return m


@pytest.fixture
def stub_message_service():
    svc = AsyncMock()
    svc.send_message = AsyncMock(
        return_value={"message_id": "msg-invoke-1", "status": "accepted"}
    )
    return svc


@pytest.fixture
def stub_audit():
    a = AsyncMock()
    a.log_event = AsyncMock()
    return a


@pytest.fixture
def stub_agent_service():
    svc = AsyncMock()
    agent = AsyncMock()
    agent.agent_id = "11111111-1111-1111-1111-111111111111"
    agent.name = "Caller"
    agent.wallet_address = None
    svc.get_agent_by_api_key = AsyncMock(return_value=agent)
    return svc


@pytest.fixture(autouse=True)
def _reset_overrides_and_limiter():
    limiter.enabled = False
    yield
    limiter.enabled = True
    app.dependency_overrides.clear()


def _wire(metrics, message_service, audit, agent_service=None) -> None:
    app.dependency_overrides[get_metrics] = lambda: metrics
    app.dependency_overrides[get_message_service] = lambda: message_service
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_agent_service] = lambda: agent_service or AsyncMock()


def test_host_invoke_delivers_as_system_agent_router(
    stub_metrics, stub_message_service, stub_audit, monkeypatch
):
    monkeypatch.setattr(
        "acn.routes.dependencies.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    )
    _wire(stub_metrics, stub_message_service, stub_audit)
    client = TestClient(app)
    callee = "22222222-2222-2222-2222-222222222222"
    resp = client.post(
        "/api/v1/invoke",
        headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
        json={
            "to": callee,
            "message": {"text": "hello"},
            "request_id": "req-host-1",
            "payer": {"kind": "human", "user_id": "auth0|alice"},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["hop_id"] == f"hop:invoke:req-host-1:{callee}"
    assert body["from"] == "system:agent-router"
    stub_message_service.send_message.assert_awaited()
    kwargs = stub_message_service.send_message.await_args.kwargs
    assert kwargs["from_agent_id"] == "system:agent-router"
    assert kwargs["to_agent_id"] == callee


def test_reject_local_agent(stub_metrics, stub_message_service, stub_audit, monkeypatch):
    monkeypatch.setattr(
        "acn.routes.dependencies.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    )
    _wire(stub_metrics, stub_message_service, stub_audit)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/invoke",
        headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
        json={
            "to": "local:cursor",
            "message": {"text": "nope"},
            "payer": {"kind": "human", "user_id": "auth0|alice"},
        },
    )
    assert resp.status_code == 400
    assert resp.json()["details"]["reason"] == "local_or_system_agent_forbidden"
    stub_message_service.send_message.assert_not_awaited()


def test_agent_invoke_uses_authenticated_from(
    stub_metrics, stub_message_service, stub_audit, stub_agent_service
):
    _wire(stub_metrics, stub_message_service, stub_audit, stub_agent_service)
    client = TestClient(app)
    callee = "22222222-2222-2222-2222-222222222222"
    with patch("acn.routes.invoke._notify_backend_complete", new=AsyncMock()):
        resp = client.post(
            "/api/v1/invoke",
            headers={"Authorization": "Bearer acn_test_key"},
            json={"to": callee, "message": {"text": "hi"}, "request_id": "req-agent-1"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["from"] == "11111111-1111-1111-1111-111111111111"
    kwargs = stub_message_service.send_message.await_args.kwargs
    assert kwargs["from_agent_id"] == "11111111-1111-1111-1111-111111111111"


def _slot_agent(agent_id: str, *, slots=None, mode="open", owner=None, name=None):
    agent = AsyncMock()
    agent.agent_id = agent_id
    agent.name = name
    agent.metadata = {"invoke_slots": slots or []}
    agent.communication_policy = {"mode": mode}
    agent.owner = owner
    return agent


def test_to_plus_slot_requires_declaration(
    stub_metrics, stub_message_service, stub_audit, monkeypatch
):
    monkeypatch.setattr(
        "acn.routes.dependencies.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    )
    callee = "22222222-2222-2222-2222-222222222222"
    agent_service = AsyncMock()
    agent_service.get_agent = AsyncMock(return_value=_slot_agent(callee))
    _wire(stub_metrics, stub_message_service, stub_audit, agent_service)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/invoke",
        headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
        json={
            "to": callee,
            "slot": "text.reply",
            "message": {"text": "hi"},
            "payer": {"kind": "human", "user_id": "auth0|alice"},
        },
    )
    assert resp.status_code == 404
    assert resp.json()["details"]["reason"] == "slot_not_declared"
    stub_message_service.send_message.assert_not_awaited()


def test_to_plus_slot_declared_delivers(
    stub_metrics, stub_message_service, stub_audit, monkeypatch
):
    monkeypatch.setattr(
        "acn.routes.dependencies.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    )
    callee = "22222222-2222-2222-2222-222222222222"
    agent_service = AsyncMock()
    declared = _slot_agent(callee, slots=[{"id": "text.reply"}])
    agent_service.get_agent = AsyncMock(return_value=declared)
    agent_service.search_agents = AsyncMock(return_value=[declared])
    agent_service.batch_alive = AsyncMock(return_value={callee})
    _wire(stub_metrics, stub_message_service, stub_audit, agent_service)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/invoke",
        headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
        json={
            "to": callee,
            "slot": "text.reply",
            "message": {"text": "hi"},
            "request_id": "req-slot-1",
            "payer": {"kind": "human", "user_id": "auth0|alice"},
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["slot"] == "text.reply"
    assert resp.json()["to"] == callee
    stub_message_service.send_message.assert_awaited()


def test_slot_only_picks_declarer(
    stub_metrics, stub_message_service, stub_audit, monkeypatch
):
    monkeypatch.setattr(
        "acn.routes.dependencies.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    )
    a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    agent_service = AsyncMock()
    agent_service.search_agents = AsyncMock(
        return_value=[
            _slot_agent(b, slots=[{"id": "text.reply"}]),
            _slot_agent(a),
        ]
    )
    agent_service.batch_alive = AsyncMock(return_value={b})
    _wire(stub_metrics, stub_message_service, stub_audit, agent_service)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/invoke",
        headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
        json={
            "slot": "text.reply",
            "message": {"text": "hi"},
            "payer": {"kind": "human", "user_id": "auth0|alice"},
            "allowed_callees": [b],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["to"] == b
    kwargs = stub_message_service.send_message.await_args.kwargs
    assert kwargs["to_agent_id"] == b


def test_slot_only_no_provider_404(
    stub_metrics, stub_message_service, stub_audit, monkeypatch
):
    monkeypatch.setattr(
        "acn.routes.dependencies.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    )
    agent_service = AsyncMock()
    agent_service.search_agents = AsyncMock(return_value=[_slot_agent("x")])
    agent_service.batch_alive = AsyncMock(return_value=set())
    _wire(stub_metrics, stub_message_service, stub_audit, agent_service)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/invoke",
        headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
        json={
            "slot": "text.reply",
            "message": {"text": "hi"},
            "payer": {"kind": "human", "user_id": "auth0|alice"},
        },
    )
    assert resp.status_code == 404
    assert resp.json()["details"]["reason"] == "no_slot_provider"
    stub_message_service.send_message.assert_not_awaited()


def test_unknown_slot_400(
    stub_metrics, stub_message_service, stub_audit, monkeypatch
):
    monkeypatch.setattr(
        "acn.routes.dependencies.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    )
    _wire(stub_metrics, stub_message_service, stub_audit)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/invoke",
        headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
        json={
            "to": "22222222-2222-2222-2222-222222222222",
            "slot": "match_collab",
            "message": {"text": "hi"},
            "payer": {"kind": "human", "user_id": "auth0|alice"},
        },
    )
    assert resp.status_code == 400
    assert resp.json()["details"]["reason"] == "unknown_slot"
    stub_message_service.send_message.assert_not_awaited()


def test_neither_to_nor_slot_422(
    stub_metrics, stub_message_service, stub_audit, monkeypatch
):
    monkeypatch.setattr(
        "acn.routes.dependencies.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    )
    _wire(stub_metrics, stub_message_service, stub_audit)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/invoke",
        headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
        json={
            "message": {"text": "hi"},
            "payer": {"kind": "human", "user_id": "auth0|alice"},
        },
    )
    assert resp.status_code == 422
    stub_message_service.send_message.assert_not_awaited()


def test_list_slot_providers(
    stub_metrics, stub_message_service, stub_audit, monkeypatch
):
    monkeypatch.setattr(
        "acn.routes.dependencies.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    )
    aid = "22222222-2222-2222-2222-222222222222"
    agent_service = AsyncMock()
    agent_service.search_agents = AsyncMock(
        return_value=[
            _slot_agent(
                aid,
                slots=[{"id": "text.reply"}],
                owner="wechat|x",
                name="Slot Desk",
            )
        ]
    )
    agent_service.batch_alive = AsyncMock(return_value={aid})
    _wire(stub_metrics, stub_message_service, stub_audit, agent_service)
    client = TestClient(app)
    resp = client.get(
        "/api/v1/invoke/slots/text.reply",
        headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["slot"]["id"] == "text.reply"
    assert body["providers"][0]["agent_id"] == aid
    assert body["providers"][0]["name"] == "Slot Desk"
    assert body["providers"][0]["online"] is True


def test_slot_fallback_on_delivery_fail(
    stub_metrics, stub_message_service, stub_audit, monkeypatch
):
    monkeypatch.setattr(
        "acn.routes.dependencies.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    )
    first = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    second = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    agent_service = AsyncMock()
    agent_service.get_agent = AsyncMock(
        return_value=_slot_agent(first, slots=[{"id": "text.reply"}])
    )
    agent_service.search_agents = AsyncMock(
        return_value=[
            _slot_agent(first, slots=[{"id": "text.reply"}]),
            _slot_agent(second, slots=[{"id": "text.reply"}]),
        ]
    )
    agent_service.batch_alive = AsyncMock(return_value={first, second})
    stub_message_service.send_message = AsyncMock(
        side_effect=[
            AgentNotFoundException("down"),
            {"message_id": "msg-fb", "status": "accepted"},
        ]
    )
    _wire(stub_metrics, stub_message_service, stub_audit, agent_service)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/invoke",
        headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
        json={
            "to": first,
            "slot": "text.reply",
            "message": {"text": "hi"},
            "request_id": "req-fb-1",
            "payer": {"kind": "human", "user_id": "auth0|alice"},
            "allowed_callees": [first, second],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["to"] == second
    assert body["fallback_from"] == first
    assert body["hop_id"] == f"hop:invoke:req-fb-1:{second}"
    assert stub_message_service.send_message.await_count == 2


def test_host_slot_without_allowlist_does_not_fallback(
    stub_metrics, stub_message_service, stub_audit, monkeypatch
):
    monkeypatch.setattr(
        "acn.routes.dependencies.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    )
    first = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    second = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    agent_service = AsyncMock()
    agent_service.get_agent = AsyncMock(
        return_value=_slot_agent(first, slots=[{"id": "text.reply"}])
    )
    agent_service.search_agents = AsyncMock(
        return_value=[
            _slot_agent(first, slots=[{"id": "text.reply"}]),
            _slot_agent(second, slots=[{"id": "text.reply"}]),
        ]
    )
    agent_service.batch_alive = AsyncMock(return_value={first, second})
    stub_message_service.send_message = AsyncMock(
        side_effect=AgentNotFoundException("down")
    )
    _wire(stub_metrics, stub_message_service, stub_audit, agent_service)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/invoke",
        headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
        json={
            "to": first,
            "slot": "text.reply",
            "message": {"text": "hi"},
            "payer": {"kind": "human", "user_id": "auth0|alice"},
        },
    )
    assert resp.status_code == 404
    assert stub_message_service.send_message.await_count == 1


def test_host_allowlist_excludes_stranger_fallback(
    stub_metrics, stub_message_service, stub_audit, monkeypatch
):
    monkeypatch.setattr(
        "acn.routes.dependencies.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    )
    first = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    stranger = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    agent_service = AsyncMock()
    agent_service.get_agent = AsyncMock(
        return_value=_slot_agent(first, slots=[{"id": "text.reply"}])
    )
    agent_service.search_agents = AsyncMock(
        return_value=[
            _slot_agent(first, slots=[{"id": "text.reply"}]),
            _slot_agent(stranger, slots=[{"id": "text.reply"}]),
        ]
    )
    agent_service.batch_alive = AsyncMock(return_value={first, stranger})
    stub_message_service.send_message = AsyncMock(
        side_effect=AgentNotFoundException("down")
    )
    _wire(stub_metrics, stub_message_service, stub_audit, agent_service)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/invoke",
        headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
        json={
            "to": first,
            "slot": "text.reply",
            "message": {"text": "hi"},
            "payer": {"kind": "human", "user_id": "auth0|alice"},
            "allowed_callees": [first],
        },
    )
    assert resp.status_code == 404
    assert stub_message_service.send_message.await_count == 1


def test_agent_slot_fallback_ignores_human_allowlist(
    stub_metrics, stub_message_service, stub_audit, stub_agent_service
):
    first = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    second = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    stub_agent_service.get_agent = AsyncMock(
        return_value=_slot_agent(first, slots=[{"id": "text.reply"}])
    )
    stub_agent_service.search_agents = AsyncMock(
        return_value=[
            _slot_agent(first, slots=[{"id": "text.reply"}]),
            _slot_agent(second, slots=[{"id": "text.reply"}]),
        ]
    )
    stub_agent_service.batch_alive = AsyncMock(return_value={first, second})
    stub_message_service.send_message = AsyncMock(
        side_effect=[
            AgentNotFoundException("down"),
            {"message_id": "msg-agent-fb", "status": "accepted"},
        ]
    )
    _wire(stub_metrics, stub_message_service, stub_audit, stub_agent_service)
    client = TestClient(app)
    with patch("acn.routes.invoke._notify_backend_complete", new=AsyncMock()):
        resp = client.post(
            "/api/v1/invoke",
            headers={"Authorization": "Bearer acn_test_key"},
            json={
                "to": first,
                "slot": "text.reply",
                "message": {"text": "hi"},
                "request_id": "req-agent-fb",
                "allowed_callees": [first],
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["to"] == second
    assert stub_message_service.send_message.await_count == 2


def test_host_allowlist_falls_back_to_closed_invitee(
    stub_metrics, stub_message_service, stub_audit, monkeypatch
):
    monkeypatch.setattr(
        "acn.routes.dependencies.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    )
    first = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    closed = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    agent_service = AsyncMock()
    agent_service.get_agent = AsyncMock(
        return_value=_slot_agent(first, slots=[{"id": "text.reply"}])
    )
    agent_service.search_agents = AsyncMock(
        return_value=[
            _slot_agent(first, slots=[{"id": "text.reply"}]),
            _slot_agent(closed, slots=[{"id": "text.reply"}], mode="closed"),
        ]
    )
    agent_service.batch_alive = AsyncMock(return_value={first, closed})
    stub_message_service.send_message = AsyncMock(
        side_effect=[
            AgentNotFoundException("down"),
            {"message_id": "msg-closed-fb", "status": "accepted"},
        ]
    )
    _wire(stub_metrics, stub_message_service, stub_audit, agent_service)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/invoke",
        headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
        json={
            "to": first,
            "slot": "text.reply",
            "message": {"text": "hi"},
            "request_id": "req-closed-fb",
            "payer": {"kind": "human", "user_id": "auth0|alice"},
            "allowed_callees": [first, closed],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["to"] == closed
    assert body["fallback_from"] == first
    assert stub_message_service.send_message.await_count == 2


def test_host_allowlist_rejects_more_than_three(
    stub_metrics, stub_message_service, stub_audit, monkeypatch
):
    monkeypatch.setattr(
        "acn.routes.dependencies.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    )
    _wire(stub_metrics, stub_message_service, stub_audit)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/invoke",
        headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
        json={
            "to": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "slot": "text.reply",
            "message": {"text": "hi"},
            "payer": {"kind": "human", "user_id": "auth0|alice"},
            "allowed_callees": [
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "dddddddd-dddd-dddd-dddd-dddddddddddd",
            ],
        },
    )
    assert resp.status_code == 422
    assert stub_message_service.send_message.await_count == 0


def test_to_only_does_not_fallback(
    stub_metrics, stub_message_service, stub_audit, monkeypatch
):
    monkeypatch.setattr(
        "acn.routes.dependencies.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    )
    stub_message_service.send_message = AsyncMock(
        side_effect=AgentNotFoundException("down")
    )
    _wire(stub_metrics, stub_message_service, stub_audit)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/invoke",
        headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
        json={
            "to": "22222222-2222-2222-2222-222222222222",
            "message": {"text": "hi"},
            "payer": {"kind": "human", "user_id": "auth0|alice"},
        },
    )
    assert resp.status_code == 404
    assert stub_message_service.send_message.await_count == 1


def test_unknown_target_404(
    stub_metrics, stub_message_service, stub_audit, monkeypatch
):
    monkeypatch.setattr(
        "acn.routes.dependencies.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    )
    stub_message_service.send_message = AsyncMock(
        side_effect=AgentNotFoundException("missing")
    )
    _wire(stub_metrics, stub_message_service, stub_audit)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/invoke",
        headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
        json={
            "to": "33333333-3333-3333-3333-333333333333",
            "message": {"text": "x"},
            "payer": {"kind": "human", "user_id": "auth0|alice"},
        },
    )
    assert resp.status_code == 404


def test_invoke_complete_forwards_when_key_is_callee(
    stub_metrics, stub_message_service, stub_audit, stub_agent_service
):
    _wire(stub_metrics, stub_message_service, stub_audit, stub_agent_service)
    client = TestClient(app)
    callee = "11111111-1111-1111-1111-111111111111"
    forwarded = AsyncMock(
        return_value={
            "status": "settled",
            "upgraded": True,
            "hop_id": f"hop:invoke:req-wb-1:{callee}",
        }
    )
    with patch("acn.routes.invoke._forward_backend_complete", new=forwarded):
        resp = client.post(
            "/api/v1/invoke/complete",
            headers={"Authorization": "Bearer acn_test_key"},
            json={
                "request_id": "req-wb-1",
                "usage": {"input_tokens": 10, "output_tokens": 4},
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["to"] == callee
    assert body["hop_id"] == f"hop:invoke:req-wb-1:{callee}"
    assert body["status"] == "settled"
    forwarded.assert_awaited()
    kwargs = forwarded.await_args.kwargs
    assert kwargs["callee"] == callee
    assert kwargs["caller"] == callee
    assert kwargs["delivery_status"] == "writeback"
    assert kwargs["usage"]["input_tokens"] == 10


def test_invoke_complete_rejects_other_agents_hop(
    stub_metrics, stub_message_service, stub_audit, stub_agent_service
):
    _wire(stub_metrics, stub_message_service, stub_audit, stub_agent_service)
    client = TestClient(app)
    forwarded = AsyncMock()
    with patch("acn.routes.invoke._forward_backend_complete", new=forwarded):
        resp = client.post(
            "/api/v1/invoke/complete",
            headers={"Authorization": "Bearer acn_test_key"},
            json={
                "hop_id": "hop:invoke:req-wb-1:22222222-2222-2222-2222-222222222222",
                "usage": {"input_tokens": 10, "output_tokens": 4},
            },
        )
    assert resp.status_code == 403
    assert resp.json()["details"]["reason"] == "invoke_complete_forbidden"
    forwarded.assert_not_awaited()


_SLOWAPI_WRAPPER_CODE_NAMES = {"async_wrapper", "sync_wrapper"}


def _has_rate_limit(endpoint) -> bool:
    fn = endpoint
    seen: set[int] = set()
    while fn is not None and id(fn) not in seen:
        seen.add(id(fn))
        code = getattr(fn, "__code__", None)
        if code is not None and code.co_name in _SLOWAPI_WRAPPER_CODE_NAMES:
            return True
        fn = getattr(fn, "__wrapped__", None)
    return False


def test_invoke_write_routes_are_rate_limited():
    from fastapi.routing import APIRoute

    found: dict[str, object] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path not in ("/api/v1/invoke", "/api/v1/invoke/complete"):
            continue
        if "POST" not in (route.methods or set()):
            continue
        found[route.path] = route.endpoint
    assert found.keys() == {"/api/v1/invoke", "/api/v1/invoke/complete"}
    for path, endpoint in found.items():
        assert _has_rate_limit(endpoint), f"{path} missing @limiter.limit"


@pytest.mark.asyncio
async def test_host_invoke_rate_key_is_payer(monkeypatch):
    from starlette.requests import Request

    from acn.routes.invoke import InvokeRequest, _authenticate_invoke

    monkeypatch.setattr(
        "acn.routes.dependencies.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    )
    request = Request(
        {
            "type": "http",
            "headers": [(b"x-internal-token", VALID_INTERNAL_TOKEN.encode())],
        }
    )
    body = InvokeRequest(
        to="22222222-2222-2222-2222-222222222222",
        message={"text": "x"},
        payer={"kind": "human", "user_id": "auth0|alice"},
    )
    await _authenticate_invoke(request, body, AsyncMock())
    assert request.state.rate_limit_key == "invoke:human:auth0|alice"


@pytest.mark.asyncio
async def test_agent_invoke_rate_key_is_agent(stub_agent_service):
    from starlette.requests import Request

    from acn.routes.invoke import InvokeRequest, _authenticate_invoke

    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", b"Bearer acn_test_key")],
        }
    )
    body = InvokeRequest(
        to="22222222-2222-2222-2222-222222222222",
        message={"text": "x"},
    )
    await _authenticate_invoke(request, body, stub_agent_service)
    assert request.state.rate_limit_key == (
        "agent:11111111-1111-1111-1111-111111111111"
    )
