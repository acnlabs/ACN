"""Route-level tests: ``SubnetInfo.owner`` is exposed by GET endpoints.

Pre-fix, ``SubnetInfo`` did not declare an ``owner`` field, so Pydantic
silently dropped it during serialization even though
``_subnet_entity_to_info`` passed ``owner=subnet.owner``. Every GET
response — single or list — omitted the field, which made it impossible
for clients (and our own ops scripts) to tell whether a subnet was
genuinely owner-less or whether the API was just hiding it.

This file pins the contract: every subnet returned by the public GET
endpoints carries a non-empty ``owner`` string. ``backend@internal`` is
the canonical placeholder for system-owned subnets (default Public
Network, per-user workspaces); user-created subnets carry the creator's
``agent_id``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.routes.dependencies import get_subnet_service


def _make_subnet_mock(
    subnet_id: str,
    *,
    owner: str,
    name: str = "Test Subnet",
    is_private: bool = False,
):
    sn = MagicMock()
    sn.subnet_id = subnet_id
    sn.name = name
    sn.owner = owner
    sn.description = None
    sn.is_private = is_private
    sn.security_config = None
    sn.metadata = {}
    sn.harness_url = None
    sn.created_at = datetime(2026, 5, 16, tzinfo=UTC)
    return sn


@pytest.fixture
def stub_subnet_service():
    svc = AsyncMock()

    user_subnet = _make_subnet_mock("subnet-user-001", owner="agent-creator")
    system_subnet = _make_subnet_mock(
        "ws-system-001",
        owner="backend@internal",
        name="workspace-system-001",
    )

    async def _list_public_subnets():
        return [user_subnet, system_subnet]

    async def _get_subnet(subnet_id: str):
        for sn in (user_subnet, system_subnet):
            if sn.subnet_id == subnet_id:
                return sn
        raise KeyError(subnet_id)

    svc.list_public_subnets = AsyncMock(side_effect=_list_public_subnets)
    svc.get_subnet = AsyncMock(side_effect=_get_subnet)
    return svc


def _wire(svc) -> None:
    app.dependency_overrides[get_subnet_service] = lambda: svc


# ---------------------------------------------------------------------------
# GET /api/v1/subnets — list
# ---------------------------------------------------------------------------


class TestListSubnetsOwnerField:
    def test_each_listed_subnet_carries_owner(self, stub_subnet_service):
        _wire(stub_subnet_service)

        with TestClient(app) as client:
            r = client.get("/api/v1/subnets")

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 2
        owners_by_id = {s["subnet_id"]: s.get("owner") for s in body["subnets"]}
        assert owners_by_id == {
            "subnet-user-001": "agent-creator",
            "ws-system-001": "backend@internal",
        }

    def test_listed_owners_are_never_empty(self, stub_subnet_service):
        """Pre-fix the field was missing entirely; tightened to also
        forbid empty strings — DB schema has owner NOT NULL, the API
        contract should match."""
        _wire(stub_subnet_service)

        with TestClient(app) as client:
            r = client.get("/api/v1/subnets")

        assert r.status_code == 200
        for s in r.json()["subnets"]:
            assert "owner" in s, f"missing owner field: {s}"
            assert s["owner"], f"empty owner for {s['subnet_id']}: {s}"


# ---------------------------------------------------------------------------
# GET /api/v1/subnets/{subnet_id} — single
# ---------------------------------------------------------------------------


class TestGetSubnetOwnerField:
    def test_user_owned_subnet_returns_creator_owner(self, stub_subnet_service):
        _wire(stub_subnet_service)

        with TestClient(app) as client:
            r = client.get("/api/v1/subnets/subnet-user-001")

        assert r.status_code == 200, r.text
        assert r.json()["owner"] == "agent-creator"

    def test_system_subnet_returns_backend_internal_owner(self, stub_subnet_service):
        _wire(stub_subnet_service)

        with TestClient(app) as client:
            r = client.get("/api/v1/subnets/ws-system-001")

        assert r.status_code == 200, r.text
        assert r.json()["owner"] == "backend@internal"
