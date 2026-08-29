"""D12-B: ``POST /agents/{id}/claim/internal`` — Host bind, not human JWT."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.entities.agent import ClaimStatus
from acn.core.exceptions import AgentNotFoundException
from acn.routes.dependencies import get_agent_service
from tests.routes.conftest import _assert_flat_shape

VALID_INTERNAL_TOKEN = "test-internal-token-min-32-chars-padding"
PATH = "/api/v1/agents/agent-target/claim/internal"


@pytest.fixture
def stub_agent_service():
    svc = AsyncMock()
    target = MagicMock()
    target.agent_id = "agent-target"
    target.name = "Target"
    target.owner = None
    target.claim_status = ClaimStatus.UNCLAIMED
    target.verification_code = "job-code"
    target.rotated_api_key = None
    target.key_invalidated = False

    async def _get_agent(agent_id: str):
        if agent_id != "agent-target":
            raise AgentNotFoundException(agent_id)
        return target

    async def _claim(*, agent_id: str, owner: str, verification_code: str | None = None):
        if not verification_code:
            raise ValueError("Claim token is required")
        if verification_code != "job-code":
            raise ValueError("Invalid claim token")
        target.owner = owner
        target.claim_status = ClaimStatus.CLAIMED
        return target

    svc.get_agent = AsyncMock(side_effect=_get_agent)
    svc.claim_agent = AsyncMock(side_effect=_claim)
    app.dependency_overrides[get_agent_service] = lambda: svc
    return svc, target


def _headers(token: str = VALID_INTERNAL_TOKEN) -> dict[str, str]:
    return {"X-Internal-Token": token}


def test_claim_internal_missing_token_401(stub_agent_service):
    client = TestClient(app)
    r = client.post(
        PATH,
        json={"owner_sub": "auth0|buyer", "verification_code": "job-code"},
    )
    assert r.status_code == 401
    body = r.json()
    _assert_flat_shape(body)
    assert body["error_code"] == "internal_token_invalid"
    stub_agent_service[0].claim_agent.assert_not_awaited()


def test_claim_internal_wrong_token_401(stub_agent_service):
    with patch(
        "acn.routes.registry.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    ):
        client = TestClient(app)
        r = client.post(
            PATH,
            headers=_headers("wrong-token-but-long-enough-padding"),
            json={"owner_sub": "auth0|buyer", "verification_code": "job-code"},
        )
    assert r.status_code == 401
    assert r.json()["error_code"] == "internal_token_invalid"
    stub_agent_service[0].claim_agent.assert_not_awaited()


def test_claim_internal_binds_with_code(stub_agent_service):
    svc, target = stub_agent_service
    with patch(
        "acn.routes.registry.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    ), patch("acn.routes.registry._grant_claim_reward"):
        client = TestClient(app)
        r = client.post(
            PATH,
            headers=_headers(),
            json={"owner_sub": "auth0|buyer", "verification_code": "job-code"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["agent_id"] == "agent-target"
    assert body["owner"] == "auth0|buyer"
    svc.claim_agent.assert_awaited_once()
    kwargs = svc.claim_agent.await_args.kwargs
    assert kwargs["owner"] == "auth0|buyer"
    assert kwargs["verification_code"] == "job-code"
    assert target.owner == "auth0|buyer"


def test_claim_internal_wrong_code_400(stub_agent_service):
    with patch(
        "acn.routes.registry.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    ):
        client = TestClient(app)
        r = client.post(
            PATH,
            headers=_headers(),
            json={"owner_sub": "auth0|buyer", "verification_code": "nope"},
        )
    assert r.status_code == 400
    body = r.json()
    _assert_flat_shape(body)
    assert body["error_code"] == "invalid_request"
    stub_agent_service[0].claim_agent.assert_awaited()


def test_claim_internal_same_owner_idempotent(stub_agent_service):
    _svc, target = stub_agent_service
    target.owner = "auth0|buyer"
    target.claim_status = ClaimStatus.CLAIMED
    with patch(
        "acn.routes.registry.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    ):
        client = TestClient(app)
        r = client.post(
            PATH,
            headers=_headers(),
            json={"owner_sub": "auth0|buyer", "verification_code": "job-code"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["owner"] == "auth0|buyer"
    stub_agent_service[0].claim_agent.assert_not_awaited()


def test_claim_internal_other_owner_409(stub_agent_service):
    _svc, target = stub_agent_service
    target.owner = "auth0|other"
    target.claim_status = ClaimStatus.CLAIMED
    with patch(
        "acn.routes.registry.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    ):
        client = TestClient(app)
        r = client.post(
            PATH,
            headers=_headers(),
            json={"owner_sub": "auth0|buyer", "verification_code": "job-code"},
        )
    assert r.status_code == 409
    assert r.json()["details"]["reason"] == "already_claimed"
    stub_agent_service[0].claim_agent.assert_not_awaited()
