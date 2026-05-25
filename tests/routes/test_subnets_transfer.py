"""Route-level tests for ``POST /api/v1/subnets/{slug}/transfer``.

Covers:
- Owner transfers to a registered agent → 200 with updated slug/owner
- Non-owner → 403 ``OWNERSHIP_MISMATCH``
- Missing subnet → 404 ``SUBNET_NOT_FOUND``
- Transfer to self → 400 ``INVALID_REQUEST``
- Transfer to unregistered agent → 400 ``INVALID_REQUEST``
- Transfer to ``backend@internal`` (ADR-0002) → 400 ``INVALID_REQUEST``
- Transfer to ``"system"`` platform identity → 400 ``INVALID_REQUEST``
- Missing auth header → 422 ``validation_failed``
- Empty ``new_owner`` string → 422 Pydantic validation failure
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import SubnetNotFoundException
from acn.routes.dependencies import (
    get_agent_service,
    get_subnet_service,
    verify_agent_api_key,
)
from tests.routes.conftest import _assert_flat_shape

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_subnet(
    slug: str = "subnet-1",
    owner: str = "agent-owner",
) -> MagicMock:
    sn = MagicMock()
    sn.slug = slug
    sn.name = "Test Subnet"
    sn.owner = owner
    sn.description = None
    sn.is_private = False
    sn.security_config = {}
    sn.created_at = MagicMock()
    sn.metadata = {}
    sn.harness_url = None
    sn.harness_registered = False
    sn.parent_slug = None
    sn.lifecycle = "persistent"
    sn.linked_task_id = None
    sn.member_agent_ids = {"agent-owner"}
    return sn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_subnet_service():
    svc = AsyncMock()
    default = _make_subnet()

    async def _transfer(slug: str, current_owner: str, new_owner: str):
        if slug != "subnet-1":
            raise SubnetNotFoundException(slug)
        if current_owner != default.owner:
            raise PermissionError(f"Owner mismatch: {current_owner} != {default.owner}")
        if new_owner == current_owner:
            raise ValueError("new_owner must differ from current_owner")
        updated = _make_subnet(slug=slug, owner=new_owner)
        return updated

    svc.transfer_owner = AsyncMock(side_effect=_transfer)
    svc._default_subnet = default
    return svc


@pytest.fixture(autouse=True)
def _wire_services(stub_subnet_service):
    stub_agent_svc = AsyncMock()
    app.dependency_overrides[get_subnet_service] = lambda: stub_subnet_service
    app.dependency_overrides[get_agent_service] = lambda: stub_agent_svc
    yield
    app.dependency_overrides.pop(get_subnet_service, None)
    app.dependency_overrides.pop(get_agent_service, None)


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def owner_client(client):
    """Client with auth pre-wired as 'agent-owner'."""
    app.dependency_overrides[verify_agent_api_key] = lambda: {"agent_id": "agent-owner"}
    yield client
    app.dependency_overrides.pop(verify_agent_api_key, None)


@pytest.fixture
def other_client(client):
    """Client with auth pre-wired as 'agent-other' (not the subnet owner)."""
    app.dependency_overrides[verify_agent_api_key] = lambda: {"agent_id": "agent-other"}
    yield client
    app.dependency_overrides.pop(verify_agent_api_key, None)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_transfer_owner_success(owner_client, stub_subnet_service):
    """Owner transfers to another agent → 200 with new owner in response."""
    resp = owner_client.post(
        "/api/v1/subnets/subnet-1/transfer",
        json={"new_owner": "agent-other"},
        headers={"Authorization": "Bearer owner-key"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["owner"] == "agent-other"
    assert body["slug"] == "subnet-1"
    stub_subnet_service.transfer_owner.assert_awaited_once_with(
        slug="subnet-1",
        current_owner="agent-owner",
        new_owner="agent-other",
    )


# ---------------------------------------------------------------------------
# Authorization errors
# ---------------------------------------------------------------------------


def test_transfer_owner_non_owner_403(other_client, stub_subnet_service):
    """Non-owner gets 403 OWNERSHIP_MISMATCH."""
    resp = other_client.post(
        "/api/v1/subnets/subnet-1/transfer",
        json={"new_owner": "agent-owner"},
        headers={"Authorization": "Bearer other-key"},
    )
    assert resp.status_code == 403, resp.text
    body = resp.json()
    _assert_flat_shape(body)
    assert body["error_code"] == "ownership_mismatch"


def test_transfer_owner_no_auth_422(client):
    """Missing auth token → 422 validation_failed (AgentApiKeyDep requires the header)."""
    resp = client.post(
        "/api/v1/subnets/subnet-1/transfer",
        json={"new_owner": "agent-other"},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error_code"] == "validation_failed"


# ---------------------------------------------------------------------------
# Not found
# ---------------------------------------------------------------------------


def test_transfer_owner_subnet_not_found_404(owner_client):
    """Unknown subnet → 404 SUBNET_NOT_FOUND."""
    resp = owner_client.post(
        "/api/v1/subnets/no-such-subnet/transfer",
        json={"new_owner": "agent-other"},
        headers={"Authorization": "Bearer owner-key"},
    )
    assert resp.status_code == 404, resp.text
    body = resp.json()
    _assert_flat_shape(body)
    assert body["error_code"] == "subnet_not_found"


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_transfer_owner_self_400(owner_client, stub_subnet_service):
    """Transferring to oneself → 400 INVALID_REQUEST."""
    resp = owner_client.post(
        "/api/v1/subnets/subnet-1/transfer",
        json={"new_owner": "agent-owner"},
        headers={"Authorization": "Bearer owner-key"},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    _assert_flat_shape(body)
    assert body["error_code"] == "invalid_request"


def test_transfer_owner_unregistered_agent_400(owner_client, stub_subnet_service):
    """Transfer to unregistered agent → 400 INVALID_REQUEST."""

    async def _transfer_unregistered(slug, current_owner, new_owner):
        if new_owner == "ghost-agent":
            raise ValueError("Agent 'ghost-agent' is not registered")
        return _make_subnet(owner=new_owner)

    stub_subnet_service.transfer_owner = AsyncMock(side_effect=_transfer_unregistered)

    resp = owner_client.post(
        "/api/v1/subnets/subnet-1/transfer",
        json={"new_owner": "ghost-agent"},
        headers={"Authorization": "Bearer owner-key"},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    _assert_flat_shape(body)
    assert body["error_code"] == "invalid_request"


def test_transfer_owner_empty_new_owner_422(owner_client):
    """Empty new_owner string fails Pydantic validation → 422."""
    resp = owner_client.post(
        "/api/v1/subnets/subnet-1/transfer",
        json={"new_owner": ""},
        headers={"Authorization": "Bearer owner-key"},
    )
    assert resp.status_code == 422, resp.text


def test_transfer_owner_backend_internal_400(owner_client, stub_subnet_service):
    """ADR-0002: transferring to 'backend@internal' → 400 INVALID_REQUEST."""

    async def _transfer_adr0002(slug, current_owner, new_owner):
        if new_owner == "backend@internal":
            raise ValueError("ADR-0002: 'backend@internal' is not a valid subnet owner")
        return _make_subnet(owner=new_owner)

    stub_subnet_service.transfer_owner = AsyncMock(side_effect=_transfer_adr0002)

    resp = owner_client.post(
        "/api/v1/subnets/subnet-1/transfer",
        json={"new_owner": "backend@internal"},
        headers={"Authorization": "Bearer owner-key"},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    _assert_flat_shape(body)
    assert body["error_code"] == "invalid_request"


def test_transfer_owner_system_identity_400(owner_client, stub_subnet_service):
    """Transferring to the reserved 'system' identity → 400 INVALID_REQUEST."""

    async def _transfer_system(slug, current_owner, new_owner):
        if new_owner == "system":
            raise ValueError("'system' is a reserved platform identity")
        return _make_subnet(owner=new_owner)

    stub_subnet_service.transfer_owner = AsyncMock(side_effect=_transfer_system)

    resp = owner_client.post(
        "/api/v1/subnets/subnet-1/transfer",
        json={"new_owner": "system"},
        headers={"Authorization": "Bearer owner-key"},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    _assert_flat_shape(body)
    assert body["error_code"] == "invalid_request"
