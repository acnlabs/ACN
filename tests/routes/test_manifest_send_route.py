"""Tests for ``POST /api/v1/communication/manifest/send`` — Phase 3 Path 2.

Pins the contract for the notify-only send endpoint:
  * Mode enforcement (manifest/allowlist only).
  * message_type validation.
  * content_url SSRF guard.
  * from_agent auth mismatch.
  * communication_profile public endpoint.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.routes.dependencies import (
    get_agent_service,
    get_message_service,
)


# --------------------------------------------------------------------------- #
# Shared fixtures / helpers
# --------------------------------------------------------------------------- #


def _make_agent(agent_id: str, mode: str = "manifest") -> MagicMock:
    agent = MagicMock()
    agent.agent_id = agent_id
    agent.communication_policy = {"mode": mode}
    agent.wallet_address = None
    return agent


@pytest.fixture
def stub_agent_service():
    svc = AsyncMock()

    sender = _make_agent("agent-sender", mode="open")
    recipient_manifest = _make_agent("agent-manifest", mode="manifest")
    recipient_open = _make_agent("agent-open", mode="open")

    async def _by_api_key(key: str):
        if key == "sender-key":
            return sender
        return None

    async def _get_agent(agent_id: str):
        if agent_id == "agent-manifest":
            return recipient_manifest
        if agent_id == "agent-open":
            return recipient_open
        if agent_id == "agent-sender":
            return sender
        from acn.services.agent_service import AgentNotFoundException

        raise AgentNotFoundException(agent_id)

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)
    svc.get_agent = AsyncMock(side_effect=_get_agent)
    return svc


@pytest.fixture
def stub_message_service():
    svc = AsyncMock()
    svc.send_message = AsyncMock(
        return_value={
            "status": "sent",
            "delivery_mode": "manifest",
            "mid": "test-mid-001",
        }
    )
    return svc


def _wire(agent_svc, msg_svc):
    app.dependency_overrides[get_agent_service] = lambda: agent_svc
    app.dependency_overrides[get_message_service] = lambda: msg_svc


def _headers(key: str = "sender-key") -> dict:
    return {"Authorization": f"Bearer {key}"}


_VALID_BODY = {
    "from_agent": "agent-sender",
    "target_agent": "agent-manifest",
    "message_type": "task_request",
    "summary": "Please review the attached report.",
}


# --------------------------------------------------------------------------- #
# POST /communication/manifest/send
# --------------------------------------------------------------------------- #


class TestManifestSend:
    def test_happy_path(self, stub_agent_service, stub_message_service):
        _wire(stub_agent_service, stub_message_service)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/communication/manifest/send",
                json=_VALID_BODY,
                headers=_headers(),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["delivery_mode"] == "manifest"
        assert body["mid"] == "test-mid-001"

    def test_open_mode_recipient_rejected(self, stub_agent_service, stub_message_service):
        """Path 2 is only for manifest/allowlist recipients."""
        _wire(stub_agent_service, stub_message_service)
        body = {**_VALID_BODY, "target_agent": "agent-open"}
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/communication/manifest/send",
                json=body,
                headers=_headers(),
            )
        assert r.status_code == 400
        rb = r.json()
        assert rb["error_code"] == "attention_fee_requires_manifest_mode"
        assert rb["details"]["actual_route"] == "open"
        stub_message_service.send_message.assert_not_awaited()

    def test_invalid_message_type_returns_422(
        self, stub_agent_service, stub_message_service
    ):
        _wire(stub_agent_service, stub_message_service)
        body = {**_VALID_BODY, "message_type": "not_a_real_type"}
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/communication/manifest/send",
                json=body,
                headers=_headers(),
            )
        assert r.status_code == 422

    def test_from_agent_mismatch_returns_403(
        self, stub_agent_service, stub_message_service
    ):
        _wire(stub_agent_service, stub_message_service)
        body = {**_VALID_BODY, "from_agent": "agent-other"}
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/communication/manifest/send",
                json=body,
                headers=_headers(),
            )
        assert r.status_code == 403
        assert r.json()["error_code"] == "from_agent_mismatch"

    def test_content_url_ssrf_blocked(self, stub_agent_service, stub_message_service):
        """Private-IP content_url must be rejected before send."""
        _wire(stub_agent_service, stub_message_service)
        body = {**_VALID_BODY, "content_url": "https://192.168.1.1/report.json"}
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/communication/manifest/send",
                json=body,
                headers=_headers(),
            )
        assert r.status_code == 400
        assert r.json()["error_code"] == "content_url_blocked"
        stub_message_service.send_message.assert_not_awaited()

    def test_valid_content_url_accepted(self, stub_agent_service, stub_message_service):
        _wire(stub_agent_service, stub_message_service)
        body = {**_VALID_BODY, "content_url": "https://cdn.example.com/report.json"}
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/communication/manifest/send",
                json=body,
                headers=_headers(),
            )
        assert r.status_code == 200

    def test_http_content_url_rejected_by_schema(
        self, stub_agent_service, stub_message_service
    ):
        _wire(stub_agent_service, stub_message_service)
        body = {**_VALID_BODY, "content_url": "http://example.com/report.json"}
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/communication/manifest/send",
                json=body,
                headers=_headers(),
            )
        assert r.status_code == 422

    def test_invalid_api_key_returns_401(
        self, stub_agent_service, stub_message_service
    ):
        _wire(stub_agent_service, stub_message_service)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/communication/manifest/send",
                json=_VALID_BODY,
                headers={"Authorization": "Bearer bad-key"},
            )
        assert r.status_code == 401

    def test_ttl_hours_passed_to_service(self, stub_agent_service, stub_message_service):
        _wire(stub_agent_service, stub_message_service)
        body = {**_VALID_BODY, "ttl_hours": 2}
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/communication/manifest/send",
                json=body,
                headers=_headers(),
            )
        assert r.status_code == 200
        call_kwargs = stub_message_service.send_message.call_args.kwargs
        assert call_kwargs.get("ttl_seconds") == 7200


# --------------------------------------------------------------------------- #
# GET /agents/{agent_id}/communication_profile
# --------------------------------------------------------------------------- #


class TestCommunicationProfile:
    def test_returns_profile_for_existing_agent(
        self, stub_agent_service, stub_message_service
    ):
        _wire(stub_agent_service, stub_message_service)
        with TestClient(app) as client:
            r = client.get("/api/v1/agents/agent-manifest/communication_profile")
        assert r.status_code == 200
        body = r.json()
        assert body["agent_id"] == "agent-manifest"
        assert body["mode"] == "manifest"
        assert body["attention_fee_required"] is False

    def test_open_agent_returns_open_mode(
        self, stub_agent_service, stub_message_service
    ):
        _wire(stub_agent_service, stub_message_service)
        with TestClient(app) as client:
            r = client.get("/api/v1/agents/agent-open/communication_profile")
        assert r.status_code == 200
        assert r.json()["mode"] == "open"

    def test_unknown_agent_returns_404(self, stub_agent_service, stub_message_service):
        _wire(stub_agent_service, stub_message_service)
        with TestClient(app) as client:
            r = client.get("/api/v1/agents/agent-unknown/communication_profile")
        assert r.status_code == 404
        assert r.json()["error_code"] == "agent_not_found"

    def test_no_auth_required(self, stub_agent_service, stub_message_service):
        """communication_profile is a public read-only endpoint."""
        _wire(stub_agent_service, stub_message_service)
        with TestClient(app) as client:
            r = client.get("/api/v1/agents/agent-manifest/communication_profile")
        assert r.status_code == 200
