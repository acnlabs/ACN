"""Route-level contract tests for ADR-0003 Phase 2.

Pins the five ``INVALID_REQUEST`` rejection variants on
``POST /api/v1/subnets``, the new ``GET /api/v1/subnets?parent=<id>``
filter, the new ``GET /api/v1/subnets/{id}/children`` endpoint, and
the new ``POST /api/v1/subnets/{id}/promote`` endpoint.

Each rejection variant pins ``error_code = "invalid_request"`` AND a
stable ``details.reason`` string that the CLI / SDK error parsers
match against — these names are part of the public contract and
shouldn't drift.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.entities import Subnet
from acn.core.exceptions import SubnetNotFoundException
from acn.routes.dependencies import (
    get_agent_service,
    get_subnet_service,
)
from acn.services.subnet_service import (
    REASON_LINKED_TASK_NOT_FOUND,
    REASON_NOT_PARENT_MEMBER,
    REASON_PARENT_IS_NESTED,
    REASON_PARENT_IS_RESERVED,
    REASON_PARENT_NOT_FOUND,
    REASON_TASK_SCOPED_REQUIRES_LINKED_TASK,
    SubnetNestingError,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_agent_service():
    svc = AsyncMock()

    target = MagicMock()
    target.agent_id = "agent-target"
    target.name = "Target"
    target.subnet_ids = []

    other = MagicMock()
    other.agent_id = "agent-other"
    other.name = "Other"

    async def _by_api_key(key: str):
        if key == "owner-key":
            return target
        if key == "other-key":
            return other
        return None

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)
    svc.get_agent = AsyncMock(return_value=target)
    svc.join_subnet = AsyncMock(return_value=None)
    svc.leave_subnet = AsyncMock(return_value=None)
    return svc


def _make_subnet_entity(
    subnet_id: str = "subnet-1",
    owner: str = "agent-target",
    *,
    is_private: bool = False,
    parent_subnet_id: str | None = None,
    lifecycle: str = "persistent",
    linked_task_id: str | None = None,
    member_agent_ids: set[str] | None = None,
) -> Subnet:
    return Subnet(
        subnet_id=subnet_id,
        name=subnet_id,
        owner=owner,
        is_private=is_private,
        parent_subnet_id=parent_subnet_id,
        lifecycle=lifecycle,  # type: ignore[arg-type]
        linked_task_id=linked_task_id,
        member_agent_ids=member_agent_ids or {owner},
        created_at=datetime.now(UTC),
    )


def _wire(agent_svc, subnet_svc) -> None:
    app.dependency_overrides[get_agent_service] = lambda: agent_svc
    app.dependency_overrides[get_subnet_service] = lambda: subnet_svc


# ---------------------------------------------------------------------------
# POST /api/v1/subnets — five rejection variants + happy path
# ---------------------------------------------------------------------------


_REASON_ERROR_CODE_MAP: dict[str, tuple[int, str]] = {
    # (status_code, error_code) per rejection reason.
    REASON_PARENT_NOT_FOUND: (400, "invalid_request"),
    REASON_PARENT_IS_RESERVED: (400, "invalid_request"),
    REASON_PARENT_IS_NESTED: (400, "invalid_request"),
    REASON_TASK_SCOPED_REQUIRES_LINKED_TASK: (400, "invalid_request"),
    REASON_LINKED_TASK_NOT_FOUND: (400, "invalid_request"),
    # not_parent_member maps to 403 NOT_SUBNET_MEMBER, see the
    # admin_add_subnet_member test below.
}


@pytest.mark.parametrize("reason", list(_REASON_ERROR_CODE_MAP))
def test_create_subnet_invariant_rejection(reason, stub_agent_service):
    """One test per ``details.reason`` string the service raises.

    Each variant is exercised by stubbing ``create_subnet`` to raise
    ``SubnetNestingError(reason)`` and asserting the route maps it to
    ``ACNHTTPError(INVALID_REQUEST, 400, details={"reason": <token>})``.
    """
    expected_status, expected_code = _REASON_ERROR_CODE_MAP[reason]
    subnet_svc = AsyncMock()
    subnet_svc.create_subnet = AsyncMock(
        side_effect=SubnetNestingError(reason, f"test reason: {reason}")
    )
    _wire(stub_agent_service, subnet_svc)

    body: dict = {"name": "Squad", "subnet_id": "squad-1"}
    # Send a body shape that *could* plausibly produce each reason —
    # the stub raises regardless, but a realistic payload helps the
    # contract test document each variant's intended call site.
    if reason in {REASON_PARENT_NOT_FOUND, REASON_PARENT_IS_RESERVED,
                  REASON_PARENT_IS_NESTED}:
        body["parent_subnet_id"] = "some-parent"
    if reason == REASON_TASK_SCOPED_REQUIRES_LINKED_TASK:
        body["lifecycle"] = "task_scoped"
    if reason == REASON_LINKED_TASK_NOT_FOUND:
        body["lifecycle"] = "task_scoped"
        body["linked_task_id"] = "task-ghost"

    with TestClient(app) as client:
        r = client.post(
            "/api/v1/subnets",
            headers={"Authorization": "Bearer owner-key"},
            json=body,
        )

    assert r.status_code == expected_status, r.text
    payload = r.json()
    assert payload["error_code"] == expected_code
    assert payload["details"]["reason"] == reason
    # Agent-side join must NOT fire when the create fails.
    stub_agent_service.join_subnet.assert_not_awaited()


def test_create_subnet_happy_path_with_nesting(stub_agent_service):
    """Successful create with all three new fields surfaces them in
    the gateway response chain and triggers the agent-side join."""
    subnet_svc = AsyncMock()

    async def _create(**kwargs):
        return _make_subnet_entity(
            subnet_id=kwargs["subnet_id"],
            owner=kwargs["owner"],
            parent_subnet_id=kwargs.get("parent_subnet_id"),
            lifecycle=kwargs.get("lifecycle", "persistent"),
            linked_task_id=kwargs.get("linked_task_id"),
        )

    subnet_svc.create_subnet = AsyncMock(side_effect=_create)
    _wire(stub_agent_service, subnet_svc)

    with TestClient(app) as client:
        r = client.post(
            "/api/v1/subnets",
            headers={"Authorization": "Bearer owner-key"},
            json={
                "name": "Bug Squad",
                "subnet_id": "squad-1",
                "parent_subnet_id": "parent-1",
                "lifecycle": "task_scoped",
                "linked_task_id": "task-42",
            },
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["subnet_id"] == "squad-1"
    # Service was called with the three nesting fields wired through
    subnet_svc.create_subnet.assert_awaited_once()
    call_kwargs = subnet_svc.create_subnet.await_args.kwargs
    assert call_kwargs["parent_subnet_id"] == "parent-1"
    assert call_kwargs["lifecycle"] == "task_scoped"
    assert call_kwargs["linked_task_id"] == "task-42"


# ---------------------------------------------------------------------------
# GET /api/v1/subnets?parent=<id> — filter + ACL
# ---------------------------------------------------------------------------


class TestListSubnetsParentFilter:
    def test_parent_filter_returns_children(self, stub_agent_service):
        children = [
            _make_subnet_entity(
                subnet_id="child-1",
                owner="agent-target",
                parent_subnet_id="parent-1",
            ),
            _make_subnet_entity(
                subnet_id="child-2",
                owner="agent-target",
                parent_subnet_id="parent-1",
            ),
        ]
        subnet_svc = AsyncMock()
        subnet_svc.list_children = AsyncMock(return_value=children)
        _wire(stub_agent_service, subnet_svc)

        with TestClient(app) as client:
            r = client.get("/api/v1/subnets?parent=parent-1")

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 2
        ids = {s["subnet_id"] for s in body["subnets"]}
        assert ids == {"child-1", "child-2"}
        # ACL: anonymous → requester_id=None
        subnet_svc.list_children.assert_awaited_once_with(
            parent_subnet_id="parent-1", requester_id=None
        )

    def test_parent_filter_no_existence_leak_returns_empty(
        self, stub_agent_service
    ):
        """Unknown parent returns ``{"count": 0, "subnets": []}`` —
        same shape as a legitimate no-children result."""
        subnet_svc = AsyncMock()
        subnet_svc.list_children = AsyncMock(return_value=[])
        _wire(stub_agent_service, subnet_svc)

        with TestClient(app) as client:
            r = client.get("/api/v1/subnets?parent=missing-parent")

        assert r.status_code == 200
        body = r.json()
        assert body == {"count": 0, "subnets": []}


# ---------------------------------------------------------------------------
# GET /api/v1/subnets/{id}/children
# ---------------------------------------------------------------------------


class TestGetSubnetChildren:
    def test_children_endpoint_returns_count_and_subnets(self, stub_agent_service):
        parent = _make_subnet_entity(subnet_id="parent-1")
        children = [
            _make_subnet_entity(
                subnet_id="child-A",
                parent_subnet_id="parent-1",
            ),
        ]
        subnet_svc = AsyncMock()

        async def _get_subnet(subnet_id: str):
            if subnet_id == "parent-1":
                return parent
            raise SubnetNotFoundException(subnet_id)

        subnet_svc.get_subnet = AsyncMock(side_effect=_get_subnet)
        subnet_svc.list_children = AsyncMock(return_value=children)
        _wire(stub_agent_service, subnet_svc)

        with TestClient(app) as client:
            r = client.get("/api/v1/subnets/parent-1/children")

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 1
        assert body["subnets"][0]["subnet_id"] == "child-A"
        assert body["subnets"][0]["parent_subnet_id"] == "parent-1"

    def test_children_endpoint_404_for_missing_parent(self, stub_agent_service):
        subnet_svc = AsyncMock()
        subnet_svc.get_subnet = AsyncMock(
            side_effect=SubnetNotFoundException("parent-missing")
        )
        _wire(stub_agent_service, subnet_svc)

        with TestClient(app) as client:
            r = client.get("/api/v1/subnets/parent-missing/children")

        assert r.status_code == 404
        body = r.json()
        assert body["error_code"] == "subnet_not_found"
        # ``list_children`` is NOT called when the parent itself is
        # missing — confirms we 404 cleanly rather than leaking
        # empty-list as "exists but no children".
        subnet_svc.list_children.assert_not_called()


# ---------------------------------------------------------------------------
# POST /api/v1/subnets/{id}/promote
# ---------------------------------------------------------------------------


class TestPromoteSubnet:
    def test_promote_returns_updated_subnet_info(self, stub_agent_service):
        promoted = _make_subnet_entity(
            subnet_id="squad-1",
            parent_subnet_id="parent-1",
            lifecycle="persistent",
            linked_task_id=None,
        )
        subnet_svc = AsyncMock()
        subnet_svc.promote_to_persistent = AsyncMock(return_value=promoted)
        _wire(stub_agent_service, subnet_svc)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/subnets/squad-1/promote",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["subnet_id"] == "squad-1"
        assert body["lifecycle"] == "persistent"
        assert body["linked_task_id"] is None
        subnet_svc.promote_to_persistent.assert_awaited_once_with(
            subnet_id="squad-1", owner="agent-target"
        )

    def test_promote_404_for_missing_subnet(self, stub_agent_service):
        subnet_svc = AsyncMock()
        subnet_svc.promote_to_persistent = AsyncMock(
            side_effect=SubnetNotFoundException("squad-ghost")
        )
        _wire(stub_agent_service, subnet_svc)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/subnets/squad-ghost/promote",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 404
        assert r.json()["error_code"] == "subnet_not_found"

    def test_promote_403_for_non_owner(self, stub_agent_service):
        subnet_svc = AsyncMock()
        subnet_svc.promote_to_persistent = AsyncMock(
            side_effect=PermissionError("Owner mismatch")
        )
        _wire(stub_agent_service, subnet_svc)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/subnets/squad-1/promote",
                headers={"Authorization": "Bearer other-key"},
            )

        assert r.status_code == 403
        assert r.json()["error_code"] == "ownership_mismatch"

    def test_promote_unauthenticated_rejected(self, stub_agent_service):
        subnet_svc = AsyncMock()
        _wire(stub_agent_service, subnet_svc)

        with TestClient(app) as client:
            r = client.post("/api/v1/subnets/squad-1/promote")

        # API-key auth gate fires before the service is reached.
        assert 400 <= r.status_code < 500
        subnet_svc.promote_to_persistent.assert_not_called()


# ---------------------------------------------------------------------------
# admin_add_subnet_member — NOT_SUBNET_MEMBER on child membership-subset
# ---------------------------------------------------------------------------


class TestAdminAddSubnetMemberNesting:
    def test_admin_add_to_child_rejects_non_parent_member(
        self, stub_agent_service, monkeypatch
    ):
        """The membership-subset invariant fires through the admin
        path too. Surfaced as ``NOT_SUBNET_MEMBER`` (403), not
        ``INVALID_REQUEST`` — see ``_nesting_error_to_acn``.
        """
        # Disable internal-token / acn:admin gate so the test
        # exercises the body of the handler.
        from acn.auth import middleware

        async def _fake_authz(*args, **kwargs):
            return {"sub": "system", "permissions": ["acn:admin"]}

        monkeypatch.setattr(
            middleware, "require_internal_or_permission", lambda _scope: _fake_authz
        )

        subnet_svc = AsyncMock()
        subnet_svc.add_member = AsyncMock(
            side_effect=SubnetNestingError(REASON_NOT_PARENT_MEMBER, "test")
        )
        _wire(stub_agent_service, subnet_svc)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/subnets/child-1/members/bob",
                headers={"X-Internal-Token": "test-internal-token-must-be-at-least-32-characters-long"},
            )

        assert r.status_code == 403, r.text
        body = r.json()
        assert body["error_code"] == "not_subnet_member"
        assert body["details"]["reason"] == REASON_NOT_PARENT_MEMBER
