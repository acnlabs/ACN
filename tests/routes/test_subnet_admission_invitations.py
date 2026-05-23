"""Route tests for ADR-0004 Slice 2.3 invitation endpoints (5 + 1).

Covers:
- POST   /api/v1/subnets/{s}/invitations                     # owner sends
- GET    /api/v1/subnets/{s}/invitations                     # owner lists
- POST   /api/v1/subnets/{s}/invitations/{iid}/accept        # invitee
- POST   /api/v1/subnets/{s}/invitations/{iid}/reject        # invitee
- DELETE /api/v1/subnets/{s}/invitations/{iid}               # owner cancels
- GET    /api/v1/agents/{a}/subnet-invitations               # invitee lists

Plus the namespace separation guard and the merge-path
(``invite_agent`` collapsing into auto-approval of a pending
join_request).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import (
    AlreadyMemberError,
    InvitationNotFoundError,
    InvitationPendingError,
)
from acn.services._join_flow_result import (
    InviteAgentMergedToApprovedJoinRequestResult,
    InviteAgentSentResult,
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


INVITATION_ID = "inv-abc"


class TestSendInvitation:
    def test_owner_sends_invitation_returns_202(self, wire):
        _, subnet_svc, _, _ = wire
        invitation = make_join_request(
            request_id=INVITATION_ID, kind="invitation"
        )
        subnet_svc.invite_agent.return_value = InviteAgentSentResult(
            subnet_id=SUBNET_ID,
            agent_id=INVITEE_AGENT_ID,
            invitation=invitation,
        )

        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/invitations",
                headers=auth_headers(OWNER_KEY),
                json={"agent_id": INVITEE_AGENT_ID, "note": "join us"},
            )

        assert r.status_code == 202, r.text
        body = r.json()
        assert body["invitation_id"] == INVITATION_ID
        assert body["status"] == "pending"
        subnet_svc.invite_agent.assert_awaited_once_with(
            SUBNET_ID,
            INVITEE_AGENT_ID,
            owner_id=OWNER_AGENT_ID,
            note="join us",
        )

    def test_merge_path_returns_200_resolved_kind_join_request(self, wire):
        """Target had pending join_request → invite collapses to auto-approval."""
        _, subnet_svc, _, _ = wire
        approved = make_join_request(
            request_id="merged-req",
            status="approved",
            decided_by=OWNER_AGENT_ID,
        )
        subnet_svc.invite_agent.return_value = (
            InviteAgentMergedToApprovedJoinRequestResult(
                subnet_id=SUBNET_ID,
                agent_id=INVITEE_AGENT_ID,
                request=approved,
            )
        )

        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/invitations",
                headers=auth_headers(OWNER_KEY),
                json={"agent_id": INVITEE_AGENT_ID},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["auto_resolved"] is True
        assert body["resolved_kind"] == "join_request"
        assert body["request_id"] == "merged-req"

    def test_non_owner_gets_403(self, wire):
        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/invitations",
                headers=auth_headers(INVITEE_KEY),
                json={"agent_id": INVITEE_AGENT_ID},
            )

        assert r.status_code == 403
        assert r.json()["error_code"] == "subnet_not_owner"

    def test_unknown_target_agent_returns_404(self, wire):
        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/invitations",
                headers=auth_headers(OWNER_KEY),
                json={"agent_id": "ghost-agent"},
            )

        assert r.status_code == 404
        assert r.json()["error_code"] == "agent_not_found"

    def test_already_member_returns_409(self, wire):
        _, subnet_svc, _, _ = wire
        subnet_svc.invite_agent.side_effect = AlreadyMemberError(
            SUBNET_ID, INVITEE_AGENT_ID
        )

        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/invitations",
                headers=auth_headers(OWNER_KEY),
                json={"agent_id": INVITEE_AGENT_ID},
            )

        assert r.status_code == 409
        assert r.json()["error_code"] == "already_member"

    def test_pending_invitation_returns_409(self, wire):
        _, subnet_svc, _, _ = wire
        subnet_svc.invite_agent.side_effect = InvitationPendingError(
            existing_invitation_id="other-inv"
        )

        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/invitations",
                headers=auth_headers(OWNER_KEY),
                json={"agent_id": INVITEE_AGENT_ID},
            )

        assert r.status_code == 409
        body = r.json()
        assert body["error_code"] == "invitation_pending"
        assert body["details"]["existing_invitation_id"] == "other-inv"


class TestAcceptInvitation:
    def test_invitee_can_accept_returns_200(self, wire):
        agent_svc, subnet_svc, _, _ = wire
        pending = make_join_request(
            request_id=INVITATION_ID,
            kind="invitation",
        )
        accepted = make_join_request(
            request_id=INVITATION_ID,
            kind="invitation",
            status="approved",
            decided_by=INVITEE_AGENT_ID,
        )
        subnet_svc.load_join_request_or_404.return_value = pending
        subnet_svc.accept_invitation.return_value = accepted

        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/invitations/{INVITATION_ID}/accept",
                headers=auth_headers(INVITEE_KEY),
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "approved"
        assert body["decided_by"] == INVITEE_AGENT_ID
        # Back-reference write must have fired on success.
        agent_svc.join_subnet.assert_awaited_once_with(
            INVITEE_AGENT_ID, SUBNET_ID
        )

    def test_non_invitee_gets_403_not_invitee(self, wire):
        _, subnet_svc, _, _ = wire
        pending = make_join_request(
            request_id=INVITATION_ID,
            kind="invitation",
            agent_id=INVITEE_AGENT_ID,
        )
        subnet_svc.load_join_request_or_404.return_value = pending

        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/invitations/{INVITATION_ID}/accept",
                headers=auth_headers(OTHER_KEY),
            )

        assert r.status_code == 403
        assert r.json()["error_code"] == "not_invitee"
        subnet_svc.accept_invitation.assert_not_awaited()

    def test_namespace_mismatch_returns_invitation_not_found(self, wire):
        """Hitting /invitations/{id} with a join_request row → 404."""
        _, subnet_svc, _, _ = wire
        subnet_svc.load_join_request_or_404.side_effect = InvitationNotFoundError(
            INVITATION_ID
        )

        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/invitations/{INVITATION_ID}/accept",
                headers=auth_headers(INVITEE_KEY),
            )

        assert r.status_code == 404
        assert r.json()["error_code"] == "invitation_not_found"


class TestRejectInvitation:
    def test_invitee_can_reject(self, wire):
        _, subnet_svc, _, _ = wire
        pending = make_join_request(
            request_id=INVITATION_ID, kind="invitation"
        )
        rejected = make_join_request(
            request_id=INVITATION_ID,
            kind="invitation",
            status="rejected",
            decided_by=INVITEE_AGENT_ID,
            note="not now",
        )
        subnet_svc.load_join_request_or_404.return_value = pending
        subnet_svc.reject_invitation.return_value = rejected

        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/invitations/{INVITATION_ID}/reject",
                headers=auth_headers(INVITEE_KEY),
                json={"note": "not now"},
            )

        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "rejected"
        assert body["note"] == "not now"

    def test_non_invitee_gets_403(self, wire):
        _, subnet_svc, _, _ = wire
        pending = make_join_request(
            request_id=INVITATION_ID, kind="invitation"
        )
        subnet_svc.load_join_request_or_404.return_value = pending

        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/subnets/{SUBNET_ID}/invitations/{INVITATION_ID}/reject",
                headers=auth_headers(OTHER_KEY),
            )

        assert r.status_code == 403
        assert r.json()["error_code"] == "not_invitee"


class TestCancelInvitation:
    def test_owner_can_cancel(self, wire):
        _, subnet_svc, _, _ = wire
        canceled = make_join_request(
            request_id=INVITATION_ID,
            kind="invitation",
            status="withdrawn",
            decided_by=OWNER_AGENT_ID,
        )
        subnet_svc.cancel_invitation.return_value = canceled

        with TestClient(app) as client:
            r = client.delete(
                f"/api/v1/subnets/{SUBNET_ID}/invitations/{INVITATION_ID}",
                headers=auth_headers(OWNER_KEY),
            )

        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "withdrawn"
        subnet_svc.cancel_invitation.assert_awaited_once()

    def test_non_owner_gets_403(self, wire):
        with TestClient(app) as client:
            r = client.delete(
                f"/api/v1/subnets/{SUBNET_ID}/invitations/{INVITATION_ID}",
                headers=auth_headers(INVITEE_KEY),
            )

        assert r.status_code == 403
        assert r.json()["error_code"] == "subnet_not_owner"


class TestListInvitations:
    def test_owner_lists_invitations(self, wire):
        _, subnet_svc, _, _ = wire
        subnet_svc.list_invitations.return_value = [
            make_join_request(request_id="inv-1", kind="invitation"),
            make_join_request(
                request_id="inv-2", kind="invitation", agent_id="agent-x"
            ),
        ]

        with TestClient(app) as client:
            r = client.get(
                f"/api/v1/subnets/{SUBNET_ID}/invitations",
                headers=auth_headers(OWNER_KEY),
            )

        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) == 2
        assert all(item["kind"] == "invitation" for item in body["items"])

    def test_status_filter_passes_through(self, wire):
        _, subnet_svc, _, _ = wire

        with TestClient(app) as client:
            r = client.get(
                f"/api/v1/subnets/{SUBNET_ID}/invitations?status=pending",
                headers=auth_headers(OWNER_KEY),
            )

        assert r.status_code == 200
        subnet_svc.list_invitations.assert_awaited_once_with(
            SUBNET_ID, status="pending", limit=100, offset=0
        )

    def test_non_owner_gets_403(self, wire):
        with TestClient(app) as client:
            r = client.get(
                f"/api/v1/subnets/{SUBNET_ID}/invitations",
                headers=auth_headers(INVITEE_KEY),
            )

        assert r.status_code == 403


class TestAgentPendingInvitations:
    def test_invitee_can_list_own_pending(self, wire):
        _, subnet_svc, _, _ = wire
        subnet_svc.list_pending_invitations_for_agent.return_value = [
            make_join_request(
                request_id="inv-1",
                kind="invitation",
                subnet_id="subnet-A",
            ),
            make_join_request(
                request_id="inv-2",
                kind="invitation",
                subnet_id="subnet-B",
            ),
        ]

        with TestClient(app) as client:
            r = client.get(
                f"/api/v1/agents/{INVITEE_AGENT_ID}/subnet-invitations",
                headers=auth_headers(INVITEE_KEY),
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["agent_id"] == INVITEE_AGENT_ID
        assert len(body["items"]) == 2

    def test_cross_agent_query_rejected_403(self, wire):
        """OTHER_KEY trying to read INVITEE's pending invitations."""
        with TestClient(app) as client:
            r = client.get(
                f"/api/v1/agents/{INVITEE_AGENT_ID}/subnet-invitations",
                headers=auth_headers(OTHER_KEY),
            )

        assert r.status_code == 403
        assert r.json()["error_code"] == "api_key_agent_mismatch"

    def test_empty_pending_list_returns_200(self, wire):
        with TestClient(app) as client:
            r = client.get(
                f"/api/v1/agents/{INVITEE_AGENT_ID}/subnet-invitations",
                headers=auth_headers(INVITEE_KEY),
            )

        assert r.status_code == 200
        assert r.json() == {"agent_id": INVITEE_AGENT_ID, "items": []}
