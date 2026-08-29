"""Tests for ``PATCH /api/v1/agents/{id}/profile``.

Closes the "agents can't edit their own basic info after registration"
gap: ``name`` / ``description`` / ``tags`` were fixed at join time, and
``PATCH /{id}`` is the A2A proxy (not a metadata route). This dedicated
sub-resource follows the same pattern as ``PATCH /{id}/policy`` and
``/social-card-url``.

Contract pinned here (route → service seam):

* **Authorization** — only the agent itself (Bearer API key) or
  ACN-internal tooling (X-Internal-Token) may edit. Anonymous and
  cross-agent callers fail before persistence.
* **Partial update** — only fields present in the body change; omitted
  fields are left untouched (PATCH, not PUT). An empty body is rejected.
* **Validation** — the shared ``_validate_agent_name`` helper rejects the
  same names registration rejects (blank / auto-generated / letterless);
  length and tag-count caps mirror the join schema.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import AgentNotFoundException
from acn.routes.dependencies import (
    _api_key_cache,
    get_agent_service,
    limiter,
)

VALID_INTERNAL_TOKEN = "test-internal-token-min-32-chars-padding"


@pytest.fixture(autouse=True)
def _reset_state():
    limiter.enabled = False
    _api_key_cache.clear()
    yield
    limiter.enabled = True
    _api_key_cache.clear()
    app.dependency_overrides.clear()


@pytest.fixture
def stub_agent_service():
    """AgentService stub. ``get_agent_by_api_key`` resolves owner-key ->
    agent-target so owner-via-API-key auth is exercisable. ``update_profile``
    echoes the merged result into a MagicMock so tests can assert what was
    persisted and which kwargs the route passed through.
    """
    svc = AsyncMock()

    target = MagicMock()
    target.agent_id = "agent-target"

    other = MagicMock()
    other.agent_id = "agent-other"

    async def _by_api_key(key: str):
        if key == "owner-key":
            return target
        if key == "other-key":
            return other
        return None

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)

    async def _update_profile(
        agent_id: str, *, name=None, description=None, tags=None, invoke_slots=None, chat_invitees=None
    ):
        if agent_id != "agent-target":
            raise AgentNotFoundException(agent_id)
        result = MagicMock()
        result.agent_id = agent_id
        # Echo merged state: provided fields win, omitted fall back to a
        # representative "stored" value so the response shape is realistic.
        result.name = name if name is not None else "Stored Name"
        result.description = (
            description if description is not None else "Stored description text."
        )
        result.tags = tags if tags is not None else ["stored"]
        result.metadata = {}
        if invoke_slots:
            result.metadata["invoke_slots"] = invoke_slots
        if chat_invitees:
            result.metadata["chat_invitees"] = chat_invitees
        return result

    svc.update_profile = AsyncMock(side_effect=_update_profile)
    return svc


def _wire(svc) -> None:
    app.dependency_overrides[get_agent_service] = lambda: svc


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #


class TestAuth:
    def test_anonymous_returns_401(self, stub_agent_service):
        _wire(stub_agent_service)
        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/profile",
                json={"name": "New Name"},
            )
        assert r.status_code == 401, r.text
        stub_agent_service.update_profile.assert_not_awaited()

    def test_cross_agent_key_returns_403(self, stub_agent_service):
        _wire(stub_agent_service)
        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/profile",
                json={"name": "New Name"},
                headers={"Authorization": "Bearer other-key"},
            )
        assert r.status_code == 403, r.text
        stub_agent_service.update_profile.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Partial update semantics
# --------------------------------------------------------------------------- #


class TestPartialUpdate:
    def test_update_name_only_leaves_others_untouched(self, stub_agent_service):
        _wire(stub_agent_service)
        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/profile",
                json={"name": "Renamed Agent"},
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 200, r.text
        kwargs = stub_agent_service.update_profile.await_args.kwargs
        assert kwargs["name"] == "Renamed Agent"
        assert kwargs["description"] is None
        assert kwargs["tags"] is None
        assert kwargs["invoke_slots"] is None
        body = r.json()
        assert body["name"] == "Renamed Agent"

    def test_update_all_fields(self, stub_agent_service):
        _wire(stub_agent_service)
        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/profile",
                json={
                    "name": "Full Update",
                    "description": "A thorough new description.",
                    "tags": ["coding", "review"],
                },
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 200, r.text
        kwargs = stub_agent_service.update_profile.await_args.kwargs
        assert kwargs["name"] == "Full Update"
        assert kwargs["description"] == "A thorough new description."
        assert kwargs["tags"] == ["coding", "review"]

    def test_clear_tags_with_empty_list(self, stub_agent_service):
        """``tags: []`` is a real update (clear all), distinct from omitting
        the field — it must reach the service as ``[]``, not ``None``."""
        _wire(stub_agent_service)
        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/profile",
                json={"tags": []},
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 200, r.text
        kwargs = stub_agent_service.update_profile.await_args.kwargs
        assert kwargs["tags"] == []
        assert kwargs["name"] is None

    def test_update_invoke_slots(self, stub_agent_service):
        _wire(stub_agent_service)
        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/profile",
                json={"invoke_slots": [{"id": "text.reply"}]},
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 200, r.text
        kwargs = stub_agent_service.update_profile.await_args.kwargs
        assert kwargs["invoke_slots"] == [
            {
                "id": "text.reply",
                "input": "text",
                "output": "text",
                "pricing": "l2_token",
            }
        ]
        assert r.json()["invoke_slots"][0]["id"] == "text.reply"

    def test_update_chat_invitees(self, stub_agent_service):
        _wire(stub_agent_service)
        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/profile",
                json={"chat_invitees": [" wechat|alice ", "wechat|alice", "wechat|bob"]},
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 200, r.text
        kwargs = stub_agent_service.update_profile.await_args.kwargs
        assert kwargs["chat_invitees"] == ["wechat|alice", "wechat|bob"]
        assert r.json()["chat_invitees"] == ["wechat|alice", "wechat|bob"]

    def test_clear_chat_invitees_with_empty_list(self, stub_agent_service):
        _wire(stub_agent_service)
        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/profile",
                json={"chat_invitees": []},
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 200, r.text
        kwargs = stub_agent_service.update_profile.await_args.kwargs
        assert kwargs["chat_invitees"] == []
        assert r.json()["chat_invitees"] == []

    def test_unknown_invoke_slot_rejected(self, stub_agent_service):
        _wire(stub_agent_service)
        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/profile",
                json={"invoke_slots": [{"id": "match_collab"}]},
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 422, r.text
        stub_agent_service.update_profile.assert_not_awaited()

    def test_empty_body_rejected(self, stub_agent_service):
        """No fields at all is a no-op request — reject with 422 so callers
        don't silently think they updated something."""
        _wire(stub_agent_service)
        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/profile",
                json={},
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 422, r.text
        stub_agent_service.update_profile.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Validation parity with registration
# --------------------------------------------------------------------------- #


class TestValidation:
    def test_auto_generated_name_rejected(self, stub_agent_service):
        """The same name rule registration enforces must apply on edit —
        otherwise an agent could rename itself to a banned shape."""
        _wire(stub_agent_service)
        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/profile",
                json={"name": "agent-1772498556"},
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 422, r.text
        stub_agent_service.update_profile.assert_not_awaited()

    def test_letterless_name_rejected(self, stub_agent_service):
        _wire(stub_agent_service)
        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/profile",
                json={"name": "12345"},
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 422, r.text

    def test_short_description_rejected(self, stub_agent_service):
        _wire(stub_agent_service)
        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/profile",
                json={"description": "short"},
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 422, r.text


# --------------------------------------------------------------------------- #
# Not found
# --------------------------------------------------------------------------- #


def test_unknown_agent_returns_404(stub_agent_service):
    _wire(stub_agent_service)
    with patch(
        "acn.routes.dependencies.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    ):
        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/ghost-agent/profile",
                json={"name": "Ghost Rename"},
                headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
            )
    assert r.status_code == 404, r.text
