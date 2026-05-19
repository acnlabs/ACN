"""Route-level tests for ``GET /api/v1/subnets/{subnet_id}`` privacy
contract — acnlabs/ACN#68.

The pre-fix handler had no ACL: anonymous callers could pull the full
``SubnetInfo`` payload (name, owner, description, ``harness_url``,
metadata) of any private subnet they could guess or learn the id of.
That contradicted the privacy contract implied by ``is_private=True``,
which is honoured everywhere else:

- ``GET /api/v1/subnets``           — private subnets filtered out
- ``GET /api/v1/subnets/{id}/agents`` — owner / admin only (401/403)
- ``GET /api/v1/subnets/{id}/children`` — caller's view filtered

This file pins the post-fix contract:

1. **Public subnets** — fully visible to anyone, no auth required.
2. **Private subnets — owner, member, and ``acn:admin``** see the
   full payload (200).
3. **Private subnets — anon, authed non-member, and any other
   caller** receive ``SUBNET_NOT_FOUND`` (404) with **byte-identical
   shape** to a genuinely missing subnet. The status-code, error
   code, and ``details`` body must not differ between
   "exists-but-hidden" and "does-not-exist", or the leak the issue
   asked us to plug is re-introduced.
4. **Genuinely missing subnets** still 404 with the same shape (no
   privacy logic should regress the not-found path).

The membership check reads ``subnet.member_agent_ids`` directly off
the loaded entity — no extra DB call — so the test fixture exposes
it as a real attribute (not a ``MagicMock`` autospec) and asserts
the route does not invent a separate roster lookup.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import SubnetNotFoundException
from acn.routes.dependencies import get_subnet_service

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_subnet(
    subnet_id: str,
    *,
    owner: str,
    is_private: bool,
    member_agent_ids: set[str] | None = None,
    name: str = "Test Subnet",
) -> MagicMock:
    sn = MagicMock()
    sn.subnet_id = subnet_id
    sn.name = name
    sn.owner = owner
    sn.description = None
    sn.is_private = is_private
    sn.security_config = None
    sn.metadata = {}
    sn.harness_url = "https://harness.example.org" if is_private else None
    sn.created_at = datetime(2026, 5, 18, tzinfo=UTC)
    sn.member_agent_ids = set(member_agent_ids or ())
    return sn


@pytest.fixture
def public_subnet() -> MagicMock:
    return _make_subnet("subnet-public", owner="agent-owner", is_private=False)


@pytest.fixture
def private_subnet() -> MagicMock:
    return _make_subnet(
        "subnet-private",
        owner="agent-owner",
        is_private=True,
        member_agent_ids={"agent-member"},
    )


@pytest.fixture
def stub_subnet_service(public_subnet, private_subnet):
    svc = AsyncMock()

    async def _get_subnet(subnet_id: str):
        for sn in (public_subnet, private_subnet):
            if sn.subnet_id == subnet_id:
                return sn
        raise SubnetNotFoundException(subnet_id)

    svc.get_subnet = AsyncMock(side_effect=_get_subnet)
    return svc


@pytest.fixture(autouse=True)
def wire(stub_subnet_service):
    app.dependency_overrides[get_subnet_service] = lambda: stub_subnet_service
    yield
    app.dependency_overrides.pop(get_subnet_service, None)


def _fake_verify_token(
    *, sub: str, permissions: list[str] | None = None
) -> Any:
    async def _impl(*args, **kwargs):
        return {"sub": sub, "permissions": permissions or []}

    return _impl


def _assert_hidden_404(body: dict) -> None:
    """Hidden-private and genuinely-missing must look identical on the
    wire. Any divergence (extra detail key, distinct ``message``,
    distinct ``error_code``) re-introduces the existence leak."""
    assert body["error_code"] == "subnet_not_found"
    assert body["details"] == {"subnet_id": "subnet-private"}


# ---------------------------------------------------------------------------
# 1. Public subnets — unconditionally visible
# ---------------------------------------------------------------------------


class TestPublicSubnetUnchanged:
    """Regression guard. The fix tightens private subnets only; the
    public path must keep returning 200 to anon callers with the same
    payload it always did."""

    def test_anon_can_read_public_subnet(self):
        with TestClient(app) as client:
            r = client.get("/api/v1/subnets/subnet-public")

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["subnet_id"] == "subnet-public"
        assert body["owner"] == "agent-owner"

    def test_authed_non_member_can_still_read_public_subnet(self, monkeypatch):
        monkeypatch.setattr(
            "acn.routes.subnets.verify_token",
            _fake_verify_token(sub="agent-random"),
        )
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/subnets/subnet-public",
                headers={"Authorization": "Bearer some-token"},
            )

        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 2. Private subnets — authorised callers see full payload
# ---------------------------------------------------------------------------


class TestPrivateSubnetAuthorisedCallers:
    def test_owner_sees_full_payload(self, monkeypatch):
        monkeypatch.setattr(
            "acn.routes.subnets.verify_token",
            _fake_verify_token(sub="agent-owner"),
        )
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/subnets/subnet-private",
                headers={"Authorization": "Bearer owner-token"},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["subnet_id"] == "subnet-private"
        assert body["owner"] == "agent-owner"
        assert body["harness_url"] == "https://harness.example.org"

    def test_member_sees_full_payload(self, monkeypatch):
        monkeypatch.setattr(
            "acn.routes.subnets.verify_token",
            _fake_verify_token(sub="agent-member"),
        )
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/subnets/subnet-private",
                headers={"Authorization": "Bearer member-token"},
            )

        assert r.status_code == 200, r.text
        assert r.json()["subnet_id"] == "subnet-private"

    def test_admin_sees_full_payload(self, monkeypatch):
        """``acn:admin`` is a platform-level permission (ops, support,
        SRE). It must override the owner/member gate so we don't
        lock support staff out of incident response on private
        subnets."""
        monkeypatch.setattr(
            "acn.routes.subnets.verify_token",
            _fake_verify_token(
                sub="agent-support",
                permissions=["acn:admin"],
            ),
        )
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/subnets/subnet-private",
                headers={"Authorization": "Bearer admin-token"},
            )

        assert r.status_code == 200, r.text
        assert r.json()["owner"] == "agent-owner"


# ---------------------------------------------------------------------------
# 3. Private subnets — unauthorised callers hidden as 404
# ---------------------------------------------------------------------------


class TestPrivateSubnetHidden:
    def test_anon_gets_subnet_not_found(self):
        with TestClient(app) as client:
            r = client.get("/api/v1/subnets/subnet-private")

        assert r.status_code == 404
        _assert_hidden_404(r.json())

    def test_authed_non_member_gets_subnet_not_found(self, monkeypatch):
        monkeypatch.setattr(
            "acn.routes.subnets.verify_token",
            _fake_verify_token(sub="agent-outsider"),
        )
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/subnets/subnet-private",
                headers={"Authorization": "Bearer outsider-token"},
            )

        assert r.status_code == 404
        _assert_hidden_404(r.json())

    def test_authed_read_only_permission_does_not_grant_admin_access(
        self, monkeypatch
    ):
        """Only the explicit ``acn:admin`` permission overrides the
        membership gate. ``acn:read`` / ``acn:write`` / any other
        scope must still 404, otherwise a generic agent token
        accidentally becomes a probe oracle."""
        monkeypatch.setattr(
            "acn.routes.subnets.verify_token",
            _fake_verify_token(
                sub="agent-reader",
                permissions=["acn:read", "acn:write"],
            ),
        )
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/subnets/subnet-private",
                headers={"Authorization": "Bearer reader-token"},
            )

        assert r.status_code == 404
        _assert_hidden_404(r.json())

    def test_hidden_and_missing_are_byte_identical(self, monkeypatch):
        """The whole point of #68: an anonymous caller cannot
        differentiate "this private subnet exists but I can't see it"
        from "this id never existed" by inspecting the response.

        We assert byte-identical status code, ``error_code``, and
        ``details`` shape across both branches.
        """
        with TestClient(app) as client:
            hidden = client.get("/api/v1/subnets/subnet-private")
            missing = client.get("/api/v1/subnets/never-existed")

        assert hidden.status_code == missing.status_code == 404
        # Mirror the same id into both detail payloads before
        # comparing — the only legitimate difference is the path
        # parameter echo, which would itself leak nothing.
        hidden_body = hidden.json()
        missing_body = missing.json()
        assert hidden_body["error_code"] == missing_body["error_code"]
        assert set(hidden_body["details"].keys()) == set(missing_body["details"].keys())
        # The shape is identical; only the subnet_id echoed back differs.
        assert hidden_body["details"] == {"subnet_id": "subnet-private"}
        assert missing_body["details"] == {"subnet_id": "never-existed"}


# ---------------------------------------------------------------------------
# 4. Genuine 404 path — regression
# ---------------------------------------------------------------------------


class TestMissingSubnetRegression:
    def test_anon_truly_missing_subnet_404(self):
        with TestClient(app) as client:
            r = client.get("/api/v1/subnets/never-existed")

        assert r.status_code == 404
        body = r.json()
        assert body["error_code"] == "subnet_not_found"
        assert body["details"] == {"subnet_id": "never-existed"}

    def test_authed_truly_missing_subnet_404(self, monkeypatch):
        """The fix must not let the auth branch swallow a real 404
        — the SubnetNotFoundException catch happens before any
        ACL evaluation, so this should never have regressed; this
        test pins that ordering."""
        monkeypatch.setattr(
            "acn.routes.subnets.verify_token",
            _fake_verify_token(sub="agent-anyone"),
        )
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/subnets/never-existed",
                headers={"Authorization": "Bearer some-token"},
            )

        assert r.status_code == 404
        assert r.json()["error_code"] == "subnet_not_found"
