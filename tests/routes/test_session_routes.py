"""Tests for ``/api/v1/sessions/*`` — Phase 3 Session layer.

Covers the route → service contract: auth boundary, state-machine
error mapping, WS push behaviour (best-effort / silently swallowed),
and self-invite guard. Storage logic lives in
``tests/services/test_session_service.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.routes.dependencies import (
    get_agent_service,
    get_session_service,
    get_ws_manager,
)
from acn.services.session_service import SessionEntry

NOW_MS = 1_700_000_000_000
EXPIRES_MS = NOW_MS + 300_000


# --------------------------------------------------------------------------- #
# Helpers / shared fixtures
# --------------------------------------------------------------------------- #


def _make_entry(
    session_id: str = "sess-abc",
    inviter_id: str = "agent-a",
    invitee_id: str = "agent-b",
    status: str = "pending",
) -> SessionEntry:
    return SessionEntry(
        session_id=session_id,
        inviter_id=inviter_id,
        invitee_id=invitee_id,
        status=status,
        created_at_ms=NOW_MS,
        expires_at_ms=EXPIRES_MS,
        metadata={},
    )


@pytest.fixture
def stub_agent_service():
    svc = AsyncMock()

    agent_a = MagicMock()
    agent_a.agent_id = "agent-a"
    agent_a.wallet_address = None

    agent_b = MagicMock()
    agent_b.agent_id = "agent-b"
    agent_b.wallet_address = None

    async def _by_api_key(key: str):
        if key == "key-a":
            return agent_a
        if key == "key-b":
            return agent_b
        return None

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)
    return svc


@pytest.fixture
def stub_session_service():
    svc = AsyncMock()
    svc.invite = AsyncMock(return_value=_make_entry())
    svc.get = AsyncMock(return_value=_make_entry())
    svc.accept = AsyncMock(return_value=_make_entry(status="accepted"))
    svc.reject = AsyncMock(return_value=_make_entry(status="rejected"))
    svc.close = AsyncMock(return_value=_make_entry(status="closed"))
    svc.list_pending = AsyncMock(return_value=[_make_entry()])
    return svc


@pytest.fixture
def stub_ws_manager():
    ws = AsyncMock()
    ws.send_to_user = AsyncMock()
    return ws


def _wire(session_svc, agent_svc, ws_mgr):
    app.dependency_overrides[get_session_service] = lambda: session_svc
    app.dependency_overrides[get_agent_service] = lambda: agent_svc
    app.dependency_overrides[get_ws_manager] = lambda: ws_mgr


def _headers(key: str = "key-a") -> dict:
    return {"Authorization": f"Bearer {key}"}


# --------------------------------------------------------------------------- #
# POST /sessions/invite/{target_agent_id}
# --------------------------------------------------------------------------- #


class TestInviteSession:
    def test_happy_path(self, stub_session_service, stub_agent_service, stub_ws_manager):
        _wire(stub_session_service, stub_agent_service, stub_ws_manager)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/sessions/invite/agent-b",
                json={},
                headers=_headers("key-a"),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["session_id"] == "sess-abc"
        assert body["status"] == "pending"
        assert body["inviter_id"] == "agent-a"
        assert body["invitee_id"] == "agent-b"
        stub_session_service.invite.assert_awaited_once()
        stub_ws_manager.send_to_user.assert_awaited_once_with(
            "agent-b",
            pytest.approx(
                {
                    "type": "session_invite",
                    "session_id": "sess-abc",
                    "from_agent": "agent-a",
                    "expires_at": EXPIRES_MS,
                },
                abs=0,
            ),
        )

    def test_self_invite_rejected(
        self, stub_session_service, stub_agent_service, stub_ws_manager
    ):
        _wire(stub_session_service, stub_agent_service, stub_ws_manager)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/sessions/invite/agent-a",  # same as authenticated agent
                json={},
                headers=_headers("key-a"),
            )
        assert r.status_code == 400
        body = r.json()
        assert body["error_code"] == "invalid_request"
        stub_session_service.invite.assert_not_awaited()

    def test_invalid_api_key_returns_401(
        self, stub_session_service, stub_agent_service, stub_ws_manager
    ):
        _wire(stub_session_service, stub_agent_service, stub_ws_manager)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/sessions/invite/agent-b",
                json={},
                headers={"Authorization": "Bearer bad-key"},
            )
        assert r.status_code == 401

    def test_with_metadata_and_ttl(
        self, stub_session_service, stub_agent_service, stub_ws_manager
    ):
        _wire(stub_session_service, stub_agent_service, stub_ws_manager)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/sessions/invite/agent-b",
                json={"ttl_seconds": 120, "metadata": {"task": "collab"}},
                headers=_headers("key-a"),
            )
        assert r.status_code == 200
        call_kwargs = stub_session_service.invite.call_args.kwargs
        assert call_kwargs["ttl_seconds"] == 120
        assert call_kwargs["metadata"] == {"task": "collab"}


# --------------------------------------------------------------------------- #
# POST /sessions/{session_id}/accept
# --------------------------------------------------------------------------- #


class TestAcceptSession:
    def test_happy_path(self, stub_session_service, stub_agent_service, stub_ws_manager):
        _wire(stub_session_service, stub_agent_service, stub_ws_manager)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/sessions/sess-abc/accept",
                headers=_headers("key-b"),  # invitee
            )
        assert r.status_code == 200
        assert r.json()["status"] == "accepted"
        stub_ws_manager.send_to_user.assert_awaited_once()
        args = stub_ws_manager.send_to_user.call_args
        assert args[0][0] == "agent-a"  # notify inviter
        assert args[0][1]["type"] == "session_accepted"

    def test_not_found_returns_404(
        self, stub_session_service, stub_agent_service, stub_ws_manager
    ):
        stub_session_service.accept = AsyncMock(return_value=None)
        _wire(stub_session_service, stub_agent_service, stub_ws_manager)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/sessions/sess-missing/accept",
                headers=_headers("key-b"),
            )
        assert r.status_code == 404
        assert r.json()["error_code"] == "session_not_found"

    def test_wrong_agent_returns_403(
        self, stub_session_service, stub_agent_service, stub_ws_manager
    ):
        stub_session_service.accept = AsyncMock(
            side_effect=PermissionError("only the invitee")
        )
        _wire(stub_session_service, stub_agent_service, stub_ws_manager)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/sessions/sess-abc/accept",
                headers=_headers("key-a"),  # inviter, not invitee
            )
        assert r.status_code == 403
        assert r.json()["error_code"] == "session_forbidden"

    def test_already_accepted_returns_400(
        self, stub_session_service, stub_agent_service, stub_ws_manager
    ):
        stub_session_service.accept = AsyncMock(
            side_effect=ValueError("Session is in status 'accepted'")
        )
        _wire(stub_session_service, stub_agent_service, stub_ws_manager)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/sessions/sess-abc/accept",
                headers=_headers("key-b"),
            )
        assert r.status_code == 400
        assert r.json()["error_code"] == "session_already_accepted"


# --------------------------------------------------------------------------- #
# POST /sessions/{session_id}/reject
# --------------------------------------------------------------------------- #


class TestRejectSession:
    def test_happy_path(self, stub_session_service, stub_agent_service, stub_ws_manager):
        _wire(stub_session_service, stub_agent_service, stub_ws_manager)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/sessions/sess-abc/reject",
                headers=_headers("key-b"),
            )
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"
        args = stub_ws_manager.send_to_user.call_args
        assert args[0][1]["type"] == "session_rejected"

    def test_not_found_returns_404(
        self, stub_session_service, stub_agent_service, stub_ws_manager
    ):
        stub_session_service.reject = AsyncMock(return_value=None)
        _wire(stub_session_service, stub_agent_service, stub_ws_manager)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/sessions/sess-missing/reject",
                headers=_headers("key-b"),
            )
        assert r.status_code == 404
        assert r.json()["error_code"] == "session_not_found"


# --------------------------------------------------------------------------- #
# DELETE /sessions/{session_id}
# --------------------------------------------------------------------------- #


class TestCloseSession:
    def test_inviter_can_close(
        self, stub_session_service, stub_agent_service, stub_ws_manager
    ):
        _wire(stub_session_service, stub_agent_service, stub_ws_manager)
        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/sessions/sess-abc",
                headers=_headers("key-a"),  # inviter
            )
        assert r.status_code == 200
        assert r.json()["status"] == "closed"
        # Notify the other party (invitee)
        args = stub_ws_manager.send_to_user.call_args
        assert args[0][0] == "agent-b"
        assert args[0][1]["type"] == "session_closed"

    def test_not_found_returns_404(
        self, stub_session_service, stub_agent_service, stub_ws_manager
    ):
        stub_session_service.close = AsyncMock(return_value=None)
        _wire(stub_session_service, stub_agent_service, stub_ws_manager)
        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/sessions/sess-gone",
                headers=_headers("key-a"),
            )
        assert r.status_code == 404
        assert r.json()["error_code"] == "session_not_found"

    def test_non_participant_returns_403(
        self, stub_session_service, stub_agent_service, stub_ws_manager
    ):
        stub_session_service.close = AsyncMock(
            side_effect=PermissionError("not a participant")
        )
        _wire(stub_session_service, stub_agent_service, stub_ws_manager)
        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/sessions/sess-abc",
                headers=_headers("key-a"),
            )
        assert r.status_code == 403
        assert r.json()["error_code"] == "session_forbidden"


# --------------------------------------------------------------------------- #
# GET /sessions/pending
# --------------------------------------------------------------------------- #


class TestListPendingSessions:
    def test_returns_pending_list(
        self, stub_session_service, stub_agent_service, stub_ws_manager
    ):
        _wire(stub_session_service, stub_agent_service, stub_ws_manager)
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/sessions/pending",
                headers=_headers("key-b"),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["agent_id"] == "agent-b"
        assert body["count"] == 1
        assert body["sessions"][0]["session_id"] == "sess-abc"

    def test_empty_returns_zero_count(
        self, stub_session_service, stub_agent_service, stub_ws_manager
    ):
        stub_session_service.list_pending = AsyncMock(return_value=[])
        _wire(stub_session_service, stub_agent_service, stub_ws_manager)
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/sessions/pending",
                headers=_headers("key-b"),
            )
        assert r.status_code == 200
        assert r.json()["count"] == 0
