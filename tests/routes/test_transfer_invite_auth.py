"""Wire tests: transfer-invite create/cancel auth matches claim/release (no acn:write)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.auth.middleware import verify_token
from acn.core.exceptions import AgentNotFoundException
from acn.routes.dependencies import get_agent_service


@pytest.fixture
def stub_agent_service():
    svc = AsyncMock()
    exp = datetime.now(UTC) + timedelta(hours=1)

    gift = MagicMock()
    gift.agent_id = "agt-gift"
    gift.owner = "owner-alice"
    gift.name = "Gift Bot"
    gift.verification_code = "vc_test_code"
    gift.transfer_invite_expires_at = MagicMock(return_value=exp)

    async def _create(agent_id: str, owner: str, ttl_seconds: int | None = None):
        if agent_id != "agt-gift":
            raise AgentNotFoundException(agent_id)
        if owner != "owner-alice":
            raise PermissionError("Only owner can create transfer invite")
        gift.claim_status = "pending_transfer"
        return gift

    async def _cancel(agent_id: str, owner: str):
        if agent_id != "agt-gift":
            raise AgentNotFoundException(agent_id)
        if owner != "owner-alice":
            raise PermissionError("Only owner can cancel transfer invite")
        return gift

    svc.create_transfer_invite = AsyncMock(side_effect=_create)
    svc.cancel_transfer_invite = AsyncMock(side_effect=_cancel)
    return svc


def _wire(svc) -> None:
    app.dependency_overrides[get_agent_service] = lambda: svc


async def _verify_owner_alice() -> dict:
    return {"sub": "owner-alice", "permissions": []}


async def _verify_owner_bob() -> dict:
    return {"sub": "owner-bob", "permissions": ["acn:write"]}


class TestTransferInviteHumanJwtAuth:
    def test_create_without_acn_write_permission(self, stub_agent_service):
        _wire(stub_agent_service)
        app.dependency_overrides[verify_token] = _verify_owner_alice

        client = TestClient(app)
        r = client.post(
            "/api/v1/agents/agt-gift/transfer-invite",
            json={"ttl_seconds": 3600},
            headers={"Authorization": "Bearer labs-spa-jwt"},
        )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["agent_id"] == "agt-gift"
        assert body["verification_code"] == "vc_test_code"
        assert body["expires_at"]

    def test_cancel_without_acn_write_permission(self, stub_agent_service):
        _wire(stub_agent_service)
        app.dependency_overrides[verify_token] = _verify_owner_alice

        client = TestClient(app)
        r = client.post(
            "/api/v1/agents/agt-gift/transfer-invite/cancel",
            headers={"Authorization": "Bearer labs-spa-jwt"},
        )

        assert r.status_code == 200, r.text
        assert r.json()["success"] is True

    def test_non_owner_still_rejected(self, stub_agent_service):
        _wire(stub_agent_service)
        app.dependency_overrides[verify_token] = _verify_owner_bob

        client = TestClient(app)
        r = client.post(
            "/api/v1/agents/agt-gift/transfer-invite",
            json={},
            headers={"Authorization": "Bearer other-user-jwt"},
        )

        assert r.status_code == 403, r.text
        assert r.json()["error_code"] == "ownership_mismatch"
