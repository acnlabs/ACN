"""Route-level tests for ``GET /api/v1/subnets/{subnet_id}`` privacy
contract.

Contract:

1. **Public subnets** — fully visible to anyone, no auth required.
2. **Private subnets — owner, member, and ``acn:admin``** see the
   full ``SubnetInfo`` payload (200).
3. **Private subnets — anon or authed non-member** receive a
   ``SubnetStub`` (200) — structural metadata only (subnet_id, name,
   is_private, parent_subnet_id, lifecycle).  Sensitive fields such as
   owner, description, harness_url, and security_schemes are omitted.
   Rationale: the subnet_id is already discoverable via public agent
   subnet_ids, so existence-hiding provides no real security; surfacing
   hierarchy metadata lets graph clients render correct topology.
4. **Genuinely missing subnets** still 404 with ``SUBNET_NOT_FOUND``.

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
    opaque_id: str | None = None,
    parent_subnet_id: str | None = None,
) -> MagicMock:
    sn = MagicMock()
    sn.subnet_id = subnet_id
    # Opaque UUID — defaults to a deterministic-per-slug value so test
    # assertions can predict which UUID a stub will surface.
    sn.id = opaque_id or f"00000000-0000-0000-0000-{subnet_id.replace('-', '')[:12]:0<12}"
    sn.name = name
    sn.owner = owner
    sn.description = None
    sn.is_private = is_private
    sn.security_config = None
    sn.metadata = {}
    sn.harness_url = "https://harness.example.org" if is_private else None
    sn.created_at = datetime(2026, 5, 18, tzinfo=UTC)
    sn.member_agent_ids = set(member_agent_ids or ())
    sn.parent_subnet_id = parent_subnet_id
    sn.lifecycle = "persistent"
    sn.linked_task_id = None
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


def _assert_stub(body: dict) -> None:
    """Discoverable-private callers must get a SubnetStub — opaque UUID
    plus minimal structural metadata. The human-readable ``subnet_id``
    slug, ``name``, and all sensitive fields must be absent."""
    # Opaque UUID is the only identifier surfaced.
    assert "id" in body and body["id"], "stub must carry the opaque UUID"
    assert body["is_private"] is True
    # Privacy contract: human-readable identifiers must be absent.
    for leak in ("subnet_id", "name", "slug"):
        assert leak not in body, f"stub must not leak {leak!r} (organisational naming)"
    # Other sensitive fields must also be absent.
    for sensitive in ("owner", "description", "harness_url", "metadata",
                      "security_schemes", "default_security",
                      "parent_subnet_id"):
        assert sensitive not in body, f"stub must not leak {sensitive!r}"


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
# 3. Private subnets — unauthorised callers receive SubnetStub (200)
# ---------------------------------------------------------------------------


class TestPrivateSubnetDiscoverable:
    """Anon and non-member callers now get structural metadata (SubnetStub)
    instead of 404.  The subnet_id is already discoverable via public
    agent subnet_ids, so existence-hiding provides no real security."""

    def test_anon_gets_stub(self):
        with TestClient(app) as client:
            r = client.get("/api/v1/subnets/subnet-private")

        assert r.status_code == 200, r.text
        _assert_stub(r.json())

    def test_authed_non_member_gets_stub(self, monkeypatch):
        monkeypatch.setattr(
            "acn.routes.subnets.verify_token",
            _fake_verify_token(sub="agent-outsider"),
        )
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/subnets/subnet-private",
                headers={"Authorization": "Bearer outsider-token"},
            )

        assert r.status_code == 200, r.text
        _assert_stub(r.json())

    def test_authed_non_admin_scope_gets_stub(self, monkeypatch):
        """``acn:read`` / ``acn:write`` do not elevate to full payload;
        only explicit membership or ``acn:admin`` does."""
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

        assert r.status_code == 200, r.text
        _assert_stub(r.json())

    def test_stub_and_missing_are_distinguishable(self):
        """Discoverable-private returns 200 (stub); genuinely missing
        returns 404.  Callers can tell the difference — that is now
        intentional, since the subnet_id was never secret."""
        with TestClient(app) as client:
            stub_resp = client.get("/api/v1/subnets/subnet-private")
            missing_resp = client.get("/api/v1/subnets/never-existed")

        assert stub_resp.status_code == 200
        assert missing_resp.status_code == 404
        assert stub_resp.json()["subnet_id"] == "subnet-private"
        assert missing_resp.json()["error_code"] == "subnet_not_found"


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
