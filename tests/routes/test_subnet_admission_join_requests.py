"""Route tests for ADR-0004 Slice 2.3 join_request endpoints (4 verbs).

Covers:
- POST   /api/v1/subnets/{s}/join-requests/{rid}/approve   # owner-only
- POST   /api/v1/subnets/{s}/join-requests/{rid}/reject    # owner-only
- DELETE /api/v1/subnets/{s}/join-requests/{rid}            # applicant withdraw
- GET    /api/v1/subnets/{s}/join-requests                  # owner-only list

Plus the namespace separation guard (calling a join-request verb
with an invitation row id → 404 ``JOIN_REQUEST_NOT_FOUND``).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import (
    JoinRequestAlreadyDecidedError,
    JoinRequestNotFoundError,
)
from tests.routes._admission_helpers import (
    INVITEE_AGENT_ID,
    INVITEE_KEY,
    OTHER_KEY,
    OWNER_AGENT_ID,
    OWNER_KEY,
    SUBNET_ID,
    auth_headers,
    make_join_request,
    stub_agent_service,
    stub_join_flow_service,
    stub_subnet_service,
    stub_webhook_service,
    wire,
)

__all__ = [
    "stub_agent_service",
    "stub_subnet_service",
    "stub_webhook_service",
    "stub_join_flow_service",
    "wire",
]


REQUEST_ID = "req-abc"


class TestApproveJoinRequest:
    def test_owner_can_approve(self, wire):
        _, subnet_svc, _, _ = wire
        approved = make_join_request(
            request_id=REQUEST_ID,
            status="approved",
            decided_by=OWNER_AGENT_ID,
        )
        subnet_svc.approve_join_request.return_value = approved

        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/join-requests/{REQUEST_ID}/approve",
                headers=auth_headers(OWNER_KEY),
                json={"note": "welcome aboard"},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["request_id"] == REQUEST_ID
        assert body["status"] == "approved"
        assert body["decided_by"] == OWNER_AGENT_ID
        subnet_svc.approve_join_request.assert_awaited_once_with(
            SUBNET_ID, REQUEST_ID, owner_id=OWNER_AGENT_ID, note="welcome aboard"
        )

    def test_approve_without_body_works(self, wire):
        _, subnet_svc, _, _ = wire
        subnet_svc.approve_join_request.return_value = make_join_request(
            request_id=REQUEST_ID,
            status="approved",
            decided_by=OWNER_AGENT_ID,
        )

        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/join-requests/{REQUEST_ID}/approve",
                headers=auth_headers(OWNER_KEY),
            )

        assert r.status_code == 200

    def test_non_owner_gets_403(self, wire):
        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/join-requests/{REQUEST_ID}/approve",
                headers=auth_headers(INVITEE_KEY),
            )

        assert r.status_code == 403
        assert r.json()["error_code"] == "subnet_not_owner"

    def test_unknown_subnet_returns_404_subnet_not_found(self, wire):
        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/ghost/join-requests/{REQUEST_ID}/approve",
                headers=auth_headers(OWNER_KEY),
            )

        assert r.status_code == 404
        assert r.json()["error_code"] == "subnet_not_found"

    def test_missing_request_returns_404_join_request_not_found(self, wire):
        _, subnet_svc, _, _ = wire
        subnet_svc.approve_join_request.side_effect = JoinRequestNotFoundError(
            REQUEST_ID
        )

        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/join-requests/{REQUEST_ID}/approve",
                headers=auth_headers(OWNER_KEY),
            )

        assert r.status_code == 404
        body = r.json()
        assert body["error_code"] == "join_request_not_found"
        assert body["details"]["request_id"] == REQUEST_ID

    def test_already_decided_returns_409(self, wire):
        _, subnet_svc, _, _ = wire
        subnet_svc.approve_join_request.side_effect = JoinRequestAlreadyDecidedError(
            REQUEST_ID, "approved"
        )

        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/join-requests/{REQUEST_ID}/approve",
                headers=auth_headers(OWNER_KEY),
            )

        assert r.status_code == 409
        body = r.json()
        assert body["error_code"] == "join_request_already_decided"
        assert body["details"]["current_status"] == "approved"

    def test_long_note_returns_422(self, wire):
        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/join-requests/{REQUEST_ID}/approve",
                headers=auth_headers(OWNER_KEY),
                json={"note": "x" * 501},
            )

        assert r.status_code == 422


class TestRejectJoinRequest:
    def test_owner_can_reject_with_note(self, wire):
        _, subnet_svc, _, _ = wire
        rejected = make_join_request(
            request_id=REQUEST_ID,
            status="rejected",
            decided_by=OWNER_AGENT_ID,
            note="not a fit",
        )
        subnet_svc.reject_join_request.return_value = rejected

        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/join-requests/{REQUEST_ID}/reject",
                headers=auth_headers(OWNER_KEY),
                json={"note": "not a fit"},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "rejected"
        assert body["note"] == "not a fit"

    def test_non_owner_gets_403(self, wire):
        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/join-requests/{REQUEST_ID}/reject",
                headers=auth_headers(INVITEE_KEY),
            )

        assert r.status_code == 403


class TestWithdrawJoinRequest:
    def test_applicant_can_withdraw_own_request(self, wire):
        _, subnet_svc, _, _ = wire
        pending = make_join_request(
            request_id=REQUEST_ID,
            initiated_by=INVITEE_AGENT_ID,
        )
        withdrawn = make_join_request(
            request_id=REQUEST_ID,
            status="withdrawn",
            initiated_by=INVITEE_AGENT_ID,
            decided_by=INVITEE_AGENT_ID,
        )
        subnet_svc.load_join_request_or_404.return_value = pending
        subnet_svc.withdraw_join_request.return_value = withdrawn

        with TestClient(app) as client:
            r = client.delete(
                f"/api/v1/subnets/{SUBNET_ID}/join-requests/{REQUEST_ID}",
                headers=auth_headers(INVITEE_KEY),
            )

        assert r.status_code == 200
        assert r.json()["status"] == "withdrawn"
        subnet_svc.withdraw_join_request.assert_awaited_once()

    def test_cross_applicant_withdraw_rejected_403(self, wire):
        """OTHER_KEY tries to withdraw a request initiated by INVITEE."""
        _, subnet_svc, _, _ = wire
        pending = make_join_request(
            request_id=REQUEST_ID,
            initiated_by=INVITEE_AGENT_ID,
        )
        subnet_svc.load_join_request_or_404.return_value = pending

        with TestClient(app) as client:
            r = client.delete(
                f"/api/v1/subnets/{SUBNET_ID}/join-requests/{REQUEST_ID}",
                headers=auth_headers(OTHER_KEY),
            )

        assert r.status_code == 403
        body = r.json()
        assert body["error_code"] == "api_key_agent_mismatch"
        subnet_svc.withdraw_join_request.assert_not_awaited()

    def test_namespace_mismatch_returns_join_request_not_found(self, wire):
        """Hitting /join-requests/{id} with an invitation id → 404."""
        _, subnet_svc, _, _ = wire
        subnet_svc.load_join_request_or_404.side_effect = JoinRequestNotFoundError(
            REQUEST_ID
        )

        with TestClient(app) as client:
            r = client.delete(
                f"/api/v1/subnets/{SUBNET_ID}/join-requests/{REQUEST_ID}",
                headers=auth_headers(INVITEE_KEY),
            )

        assert r.status_code == 404
        assert r.json()["error_code"] == "join_request_not_found"


class TestListJoinRequests:
    def test_owner_lists_pending_join_requests(self, wire):
        _, subnet_svc, _, _ = wire
        subnet_svc.list_join_requests.return_value = [
            make_join_request(request_id="req-1"),
            make_join_request(request_id="req-2", agent_id="agent-x"),
        ]

        with TestClient(app) as client:
            r = client.get(
                f"/api/v1/subnets/{SUBNET_ID}/join-requests",
                headers=auth_headers(OWNER_KEY),
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["subnet_id"] == SUBNET_ID
        assert len(body["items"]) == 2
        # Default kind filter is join_request.
        subnet_svc.list_join_requests.assert_awaited_once_with(
            SUBNET_ID, kind="join_request", status=None, limit=100, offset=0
        )

    def test_kind_invitation_filter_rejected_400(self, wire):
        """ADR §"Application-side endpoints" — kind=invitation is forbidden."""
        with TestClient(app) as client:
            r = client.get(
                f"/api/v1/subnets/{SUBNET_ID}/join-requests?kind=invitation",
                headers=auth_headers(OWNER_KEY),
            )

        assert r.status_code == 400
        body = r.json()
        assert body["error_code"] == "invalid_kind_filter"
        assert body["details"]["kind"] == "invitation"

    def test_status_filter_passes_through(self, wire):
        _, subnet_svc, _, _ = wire

        with TestClient(app) as client:
            r = client.get(
                f"/api/v1/subnets/{SUBNET_ID}/join-requests?status=rejected",
                headers=auth_headers(OWNER_KEY),
            )

        assert r.status_code == 200
        subnet_svc.list_join_requests.assert_awaited_once_with(
            SUBNET_ID,
            kind="join_request",
            status="rejected",
            limit=100,
            offset=0,
        )

    def test_non_owner_gets_403(self, wire):
        with TestClient(app) as client:
            r = client.get(
                f"/api/v1/subnets/{SUBNET_ID}/join-requests",
                headers=auth_headers(INVITEE_KEY),
            )

        assert r.status_code == 403
        assert r.json()["error_code"] == "subnet_not_owner"

    def test_kind_allowlist_auto_allowed(self, wire):
        _, subnet_svc, _, _ = wire

        with TestClient(app) as client:
            r = client.get(
                f"/api/v1/subnets/{SUBNET_ID}/join-requests?kind=allowlist_auto",
                headers=auth_headers(OWNER_KEY),
            )

        assert r.status_code == 200
        subnet_svc.list_join_requests.assert_awaited_once_with(
            SUBNET_ID,
            kind="allowlist_auto",
            status=None,
            limit=100,
            offset=0,
        )
