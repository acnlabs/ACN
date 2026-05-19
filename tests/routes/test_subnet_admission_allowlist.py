"""Route tests for ADR-0004 Slice 2.3 allowlist endpoints (3 verbs).

Covers:
- POST   /api/v1/subnets/{s}/allowlist                  # owner-only
- DELETE /api/v1/subnets/{s}/allowlist/{agent_id}       # owner-only
- GET    /api/v1/subnets/{s}/allowlist                  # owner-only

Per ADR §"Allowlist endpoints" and §"Authorization matrix".
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import AllowlistEntryExistsError
from tests.routes._admission_helpers import (
    INVITEE_AGENT_ID,
    INVITEE_KEY,
    OTHER_KEY,
    OWNER_KEY,
    SUBNET_ID,
    auth_headers,
    make_allowlist_entry,
    stub_agent_service,
    stub_join_flow_service,
    stub_subnet_service,
    stub_webhook_service,
    wire,
)
from tests.routes.conftest import _assert_flat_shape

# Re-export so pytest discovers them as fixtures in this module.
__all__ = [
    "stub_agent_service",
    "stub_subnet_service",
    "stub_webhook_service",
    "stub_join_flow_service",
    "wire",
]


class TestAddToAllowlist:
    def test_owner_can_add_entry_returns_201(self, wire):
        _, subnet_svc, _, _ = wire
        subnet_svc.add_allowlist.return_value = make_allowlist_entry()

        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/allowlist",
                headers=auth_headers(OWNER_KEY),
                json={"agent_id": INVITEE_AGENT_ID},
            )

        assert r.status_code == 201, r.text
        body = r.json()
        assert body["subnet_id"] == SUBNET_ID
        assert body["agent_id"] == INVITEE_AGENT_ID
        assert body["added_by"] == "agent-owner"
        subnet_svc.add_allowlist.assert_awaited_once()

    def test_non_owner_gets_403(self, wire):
        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/allowlist",
                headers=auth_headers(INVITEE_KEY),
                json={"agent_id": INVITEE_AGENT_ID},
            )

        assert r.status_code == 403
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "subnet_not_owner"

    def test_unknown_subnet_returns_404(self, wire):
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/subnets/ghost/allowlist",
                headers=auth_headers(OWNER_KEY),
                json={"agent_id": INVITEE_AGENT_ID},
            )

        assert r.status_code == 404
        assert r.json()["error_code"] == "subnet_not_found"

    def test_unknown_target_agent_returns_404(self, wire):
        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/allowlist",
                headers=auth_headers(OWNER_KEY),
                json={"agent_id": "ghost-agent"},
            )

        assert r.status_code == 404
        body = r.json()
        assert body["error_code"] == "agent_not_found"
        assert body["details"]["agent_id"] == "ghost-agent"

    def test_duplicate_entry_returns_409_already_on_allowlist(self, wire):
        _, subnet_svc, _, _ = wire
        subnet_svc.add_allowlist.side_effect = AllowlistEntryExistsError(
            SUBNET_ID, INVITEE_AGENT_ID
        )

        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/allowlist",
                headers=auth_headers(OWNER_KEY),
                json={"agent_id": INVITEE_AGENT_ID},
            )

        assert r.status_code == 409
        body = r.json()
        assert body["error_code"] == "already_on_allowlist"
        assert body["details"]["subnet_id"] == SUBNET_ID
        assert body["details"]["agent_id"] == INVITEE_AGENT_ID

    def test_missing_body_returns_422(self, wire):
        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/allowlist",
                headers=auth_headers(OWNER_KEY),
            )

        # 422 from Pydantic body validation — caller did not supply
        # the required ``agent_id`` field.
        assert r.status_code == 422

    def test_unauthenticated_request_rejected(self, wire):
        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/allowlist",
                json={"agent_id": INVITEE_AGENT_ID},
            )

        assert 400 <= r.status_code < 500


class TestRemoveFromAllowlist:
    def test_owner_can_remove_entry_returns_204(self, wire):
        _, subnet_svc, _, _ = wire

        with TestClient(app) as client:
            r = client.delete(
                f"/api/v1/subnets/{SUBNET_ID}/allowlist/{INVITEE_AGENT_ID}",
                headers=auth_headers(OWNER_KEY),
            )

        assert r.status_code == 204
        # Empty body on 204 — TestClient surfaces ``.content == b""``.
        assert r.content == b""
        subnet_svc.remove_allowlist.assert_awaited_once_with(
            SUBNET_ID, INVITEE_AGENT_ID, remover="agent-owner"
        )

    def test_remove_is_idempotent_returns_204_even_when_missing(self, wire):
        """ADR §"Allowlist endpoints" — DELETE is idempotent."""
        _, subnet_svc, _, _ = wire
        # Service returns ``False`` when the pair wasn't present;
        # route still returns 204.
        subnet_svc.remove_allowlist.return_value = False

        with TestClient(app) as client:
            r = client.delete(
                f"/api/v1/subnets/{SUBNET_ID}/allowlist/{INVITEE_AGENT_ID}",
                headers=auth_headers(OWNER_KEY),
            )

        assert r.status_code == 204

    def test_non_owner_gets_403(self, wire):
        with TestClient(app) as client:
            r = client.delete(
                f"/api/v1/subnets/{SUBNET_ID}/allowlist/{INVITEE_AGENT_ID}",
                headers=auth_headers(OTHER_KEY),
            )

        assert r.status_code == 403
        assert r.json()["error_code"] == "subnet_not_owner"

    def test_unknown_subnet_returns_404(self, wire):
        with TestClient(app) as client:
            r = client.delete(
                f"/api/v1/subnets/ghost/allowlist/{INVITEE_AGENT_ID}",
                headers=auth_headers(OWNER_KEY),
            )

        assert r.status_code == 404


class TestListAllowlist:
    def test_owner_can_list_entries(self, wire):
        _, subnet_svc, _, _ = wire
        subnet_svc.list_allowlist.return_value = [
            make_allowlist_entry(agent_id="agent-a"),
            make_allowlist_entry(agent_id="agent-b"),
        ]

        with TestClient(app) as client:
            r = client.get(
                f"/api/v1/subnets/{SUBNET_ID}/allowlist",
                headers=auth_headers(OWNER_KEY),
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["subnet_id"] == SUBNET_ID
        assert len(body["entries"]) == 2
        assert {e["agent_id"] for e in body["entries"]} == {"agent-a", "agent-b"}

    def test_empty_list_returns_200(self, wire):
        with TestClient(app) as client:
            r = client.get(
                f"/api/v1/subnets/{SUBNET_ID}/allowlist",
                headers=auth_headers(OWNER_KEY),
            )

        assert r.status_code == 200
        assert r.json() == {"subnet_id": SUBNET_ID, "entries": []}

    def test_non_owner_gets_403(self, wire):
        """ADR §"GET /subnets/{s}/allowlist is owner-only deliberately"."""
        with TestClient(app) as client:
            r = client.get(
                f"/api/v1/subnets/{SUBNET_ID}/allowlist",
                headers=auth_headers(INVITEE_KEY),
            )

        assert r.status_code == 403
        assert r.json()["error_code"] == "subnet_not_owner"

    def test_pagination_params_pass_through(self, wire):
        _, subnet_svc, _, _ = wire

        with TestClient(app) as client:
            r = client.get(
                f"/api/v1/subnets/{SUBNET_ID}/allowlist?limit=50&offset=10",
                headers=auth_headers(OWNER_KEY),
            )

        assert r.status_code == 200
        subnet_svc.list_allowlist.assert_awaited_once_with(
            SUBNET_ID, limit=50, offset=10
        )

    def test_limit_capped_at_500(self, wire):
        with TestClient(app) as client:
            r = client.get(
                f"/api/v1/subnets/{SUBNET_ID}/allowlist?limit=10000",
                headers=auth_headers(OWNER_KEY),
            )

        # Pydantic rejects with 422 — pagination ceiling enforced.
        assert r.status_code == 422
