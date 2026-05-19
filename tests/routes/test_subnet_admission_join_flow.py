"""Route tests for ADR-0004 Slice 2.3 join-entry six-branch decision tree.

Covers ``POST /api/v1/agents/{agent_id}/subnets/{subnet_id}`` —
the rewritten join handler that now dispatches via
``JoinFlowService.join_subnet``. The six branches per ADR §"POST
/api/v1/agents/{agent_id}/subnets/{subnet_id} (join entry)":

  1. open subnet → 200 ``{status: "joined"}``
  2. approval subnet, caller == owner → 200 ``{status: "joined"}``
  3. approval subnet, pending invitation → 200 ``{auto_resolved,
     resolved_kind: "invitation", via: "self_join"}``
  4. approval subnet, allowlist hit + pending invitation → 200
     ``{auto_resolved, resolved_kind: "invitation", via:
     "allowlist"}``
  5. approval subnet, allowlist hit, no invitation → 200
     ``{request_id, via: "allowlist"}``
  6. approval subnet, fall-through → 202 ``{request_id, status:
     "pending"}``

Plus ALREADY_MEMBER (409) and JoinFlowError surface tests.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import AlreadyMemberError
from acn.services._join_flow_result import (
    JoinFlowAllowlistAutoApprovedResult,
    JoinFlowAutoAcceptedInvitationResult,
    JoinFlowJoinedAsOwnerResult,
    JoinFlowJoinedOpenResult,
    JoinFlowPendingResult,
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


JOIN_PATH = f"/api/v1/agents/{INVITEE_AGENT_ID}/subnets/{SUBNET_ID}"


class TestBranch1OpenSubnet:
    def test_open_subnet_returns_200_joined(self, wire):
        agent_svc, _, _, jfs = wire
        jfs.join_subnet.return_value = JoinFlowJoinedOpenResult(
            subnet_id=SUBNET_ID,
            agent_id=INVITEE_AGENT_ID,
        )

        with TestClient(app) as client:
            r = client.post(JOIN_PATH, headers=auth_headers(INVITEE_KEY))

        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {
            "status": "joined",
            "subnet_id": SUBNET_ID,
            "agent_id": INVITEE_AGENT_ID,
        }
        # Back-reference must be written on every 200 branch.
        agent_svc.join_subnet.assert_awaited_once_with(INVITEE_AGENT_ID, SUBNET_ID)


class TestBranch2OwnerSelfJoin:
    def test_owner_self_join_returns_200_joined(self, wire):
        # Owner authenticates as themselves and joins their own subnet.
        agent_svc, _, _, jfs = wire
        jfs.join_subnet.return_value = JoinFlowJoinedAsOwnerResult(
            subnet_id=SUBNET_ID,
            agent_id=OWNER_AGENT_ID,
        )

        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/agents/{OWNER_AGENT_ID}/subnets/{SUBNET_ID}",
                headers=auth_headers(OWNER_KEY),
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "joined"
        assert body["agent_id"] == OWNER_AGENT_ID
        agent_svc.join_subnet.assert_awaited_once_with(OWNER_AGENT_ID, SUBNET_ID)


class TestBranch3SelfJoinWithPendingInvitation:
    def test_pending_invitation_auto_accepts_via_self_join(self, wire):
        agent_svc, _, _, jfs = wire
        invitation = make_join_request(
            request_id="inv-self", kind="invitation"
        )
        jfs.join_subnet.return_value = JoinFlowAutoAcceptedInvitationResult(
            subnet_id=SUBNET_ID,
            agent_id=INVITEE_AGENT_ID,
            invitation=invitation,
            via="self_join",
        )

        with TestClient(app) as client:
            r = client.post(JOIN_PATH, headers=auth_headers(INVITEE_KEY))

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["auto_resolved"] is True
        assert body["resolved_kind"] == "invitation"
        assert body["invitation_id"] == "inv-self"
        assert body["via"] == "self_join"
        agent_svc.join_subnet.assert_awaited_once()


class TestBranch4AllowlistAndPendingInvitation:
    def test_via_allowlist_with_invitation_collapses_to_invitation(self, wire):
        agent_svc, _, _, jfs = wire
        invitation = make_join_request(
            request_id="inv-merged", kind="invitation"
        )
        jfs.join_subnet.return_value = JoinFlowAutoAcceptedInvitationResult(
            subnet_id=SUBNET_ID,
            agent_id=INVITEE_AGENT_ID,
            invitation=invitation,
            via="allowlist",
        )

        with TestClient(app) as client:
            r = client.post(JOIN_PATH, headers=auth_headers(INVITEE_KEY))

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["resolved_kind"] == "invitation"
        assert body["invitation_id"] == "inv-merged"
        assert body["via"] == "allowlist"
        # ``request_id`` MUST NOT appear — ADR §"Merge-path event
        # mapping" forbids creating an ``allowlist_auto`` row when
        # an invitation is collapsed.
        assert "request_id" not in body
        agent_svc.join_subnet.assert_awaited_once()


class TestBranch5AllowlistAutoApproved:
    def test_allowlist_hit_no_invitation_creates_allowlist_auto(self, wire):
        agent_svc, _, _, jfs = wire
        auto_row = make_join_request(
            request_id="auto-1",
            kind="allowlist_auto",
            status="approved",
            initiated_by="system:allowlist",
            decided_by="system:allowlist",
        )
        jfs.join_subnet.return_value = JoinFlowAllowlistAutoApprovedResult(
            subnet_id=SUBNET_ID,
            agent_id=INVITEE_AGENT_ID,
            request=auto_row,
        )

        with TestClient(app) as client:
            r = client.post(JOIN_PATH, headers=auth_headers(INVITEE_KEY))

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["request_id"] == "auto-1"
        assert body["via"] == "allowlist"
        agent_svc.join_subnet.assert_awaited_once()


class TestBranch6PendingJoinRequest:
    def test_fall_through_creates_pending_join_request_202(self, wire):
        agent_svc, _, _, jfs = wire
        pending = make_join_request(request_id="req-pending")
        jfs.join_subnet.return_value = JoinFlowPendingResult(
            subnet_id=SUBNET_ID,
            agent_id=INVITEE_AGENT_ID,
            request=pending,
        )

        with TestClient(app) as client:
            r = client.post(JOIN_PATH, headers=auth_headers(INVITEE_KEY))

        assert r.status_code == 202, r.text
        body = r.json()
        assert body["request_id"] == "req-pending"
        assert body["status"] == "pending"
        # Critical contract: branch 6 must NOT write the agent-side
        # back-reference (caller is not yet a member).
        agent_svc.join_subnet.assert_not_awaited()


class TestErrorSurfaces:
    def test_already_member_returns_409_already_member(self, wire):
        _, _, _, jfs = wire
        jfs.join_subnet.side_effect = AlreadyMemberError(
            SUBNET_ID, INVITEE_AGENT_ID
        )

        with TestClient(app) as client:
            r = client.post(JOIN_PATH, headers=auth_headers(INVITEE_KEY))

        assert r.status_code == 409, r.text
        body = r.json()
        assert body["error_code"] == "already_member"
        assert body["details"]["subnet_id"] == SUBNET_ID
        assert body["details"]["agent_id"] == INVITEE_AGENT_ID

    def test_path_agent_mismatch_returns_403_before_dispatch(self, wire):
        """API key for INVITEE → path agent_id == OTHER → 403 + no dispatch."""
        _, _, _, jfs = wire

        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/agents/{OWNER_AGENT_ID}/subnets/{SUBNET_ID}",
                headers=auth_headers(OTHER_KEY),
            )

        assert r.status_code == 403
        assert r.json()["error_code"] == "api_key_agent_mismatch"
        # JoinFlowService must not be invoked when authz fails up-front.
        jfs.join_subnet.assert_not_awaited()

    def test_missing_subnet_returns_404(self, wire):
        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/agents/{INVITEE_AGENT_ID}/subnets/ghost",
                headers=auth_headers(INVITEE_KEY),
            )

        assert r.status_code == 404
        assert r.json()["error_code"] == "subnet_not_found"
