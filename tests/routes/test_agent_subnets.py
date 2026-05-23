"""Route-level tests for the canonical agent-side subnet membership API.

Covers the new canonical paths introduced in this batch:
- ``POST   /api/v1/agents/{agent_id}/subnets/{slug}`` — join
- ``DELETE /api/v1/agents/{agent_id}/subnets/{slug}`` — leave
- ``GET    /api/v1/agents/{agent_id}/subnets`` — list

The legacy paths under ``/api/v1/subnets/{agent_id}/subnets/…`` are still
served by ``routes/subnets.py`` (marked ``deprecated=True``); both
surfaces share business logic via ``routes/_subnet_membership.py``. The
last test in this file (``TestPathEquivalence``) is the explicit
contract guard: same input → byte-for-byte same response, same
side-effects, on both URL shapes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import AgentNotFoundException, SubnetNotFoundException
from acn.protocols.ap2.webhook import WebhookEventType
from acn.routes.dependencies import (
    get_agent_service,
    get_join_flow_service,
    get_subnet_service,
    get_webhook_service,
)
from acn.services._join_flow_result import JoinFlowJoinedOpenResult
from tests.routes.conftest import _assert_flat_shape

# ---------------------------------------------------------------------------
# Fixtures (mirror tests/routes/test_subnets_harness.py for behavioural parity)
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_agent_service():
    """``owner-key`` → ``agent-target``; ``other-key`` → ``agent-other``."""
    svc = AsyncMock()

    target = MagicMock()
    target.agent_id = "agent-target"
    target.name = "Target"
    target.subnet_ids = ["subnet-1", "subnet-2"]

    other = MagicMock()
    other.agent_id = "agent-other"
    other.name = "Other"

    async def _by_api_key(key: str):
        return {"owner-key": target, "other-key": other}.get(key)

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)
    svc.get_agent = AsyncMock(return_value=target)
    svc.join_subnet = AsyncMock(return_value=None)
    svc.leave_subnet = AsyncMock(return_value=None)
    return svc


def _make_subnet_mock(
    slug: str = "subnet-1",
    owner: str = "agent-target",
    harness_url: str | None = None,
    harness_secret: str | None = None,
):
    sn = MagicMock()
    sn.slug = slug
    sn.owner = owner
    sn.harness_url = harness_url
    sn.harness_secret = harness_secret
    sn.is_private = False
    sn.member_agent_ids = set()
    return sn


@pytest.fixture
def stub_subnet_service():
    svc = AsyncMock()
    default = _make_subnet_mock()

    async def _get_subnet(slug: str):
        if slug == "subnet-1":
            return default
        raise SubnetNotFoundException(slug)

    svc.get_subnet = AsyncMock(side_effect=_get_subnet)
    svc.add_member = AsyncMock(return_value=None)
    svc.remove_member = AsyncMock(return_value=None)
    svc._default_subnet = default
    return svc


@pytest.fixture
def stub_webhook_service():
    svc = AsyncMock()
    svc.send_to = AsyncMock(return_value=True)
    svc.send_event = AsyncMock(return_value=True)
    return svc


@pytest.fixture(autouse=True)
def stub_join_flow_service(stub_subnet_service):
    """JoinFlowService stub: delegates to the subnet_service mock.

    Auto-applied to every test in this module because the canonical
    ``POST /api/v1/agents/{a}/subnets/{s}`` route now depends on
    ``JoinFlowService`` (ADR-0004 Slice 2.3 rewrote
    ``do_join_subnet`` to use the six-branch decision tree). Tests
    that don't exercise join still pick this fixture up — it is a
    no-op for leave / list paths because the routes never call
    ``join_flow_service.join_subnet`` outside the join handler.

    The stub mirrors the open-branch behaviour of the real service:
    it validates the subnet exists (via the underlying subnet
    service stub) and calls ``add_member``, then returns a
    ``JoinFlowJoinedOpenResult``. Pre-existing assertions on
    ``stub_subnet_service.add_member.assert_awaited_once_with(...)``
    continue to hold because the forwarding preserves the call.
    """
    svc = AsyncMock()

    async def _join_subnet(slug: str, agent_id: str):
        await stub_subnet_service.get_subnet(slug)
        await stub_subnet_service.add_member(slug, agent_id)
        return JoinFlowJoinedOpenResult(slug=slug, agent_id=agent_id)

    svc.join_subnet = AsyncMock(side_effect=_join_subnet)
    app.dependency_overrides[get_join_flow_service] = lambda: svc
    try:
        yield svc
    finally:
        app.dependency_overrides.pop(get_join_flow_service, None)


def _wire(agent_svc, subnet_svc, webhook_svc=None) -> None:
    app.dependency_overrides[get_agent_service] = lambda: agent_svc
    app.dependency_overrides[get_subnet_service] = lambda: subnet_svc
    if webhook_svc is not None:
        app.dependency_overrides[get_webhook_service] = lambda: webhook_svc


# ---------------------------------------------------------------------------
# POST /api/v1/agents/{agent_id}/subnets/{slug} — canonical join
# ---------------------------------------------------------------------------


class TestCanonicalJoin:
    def test_join_succeeds_under_canonical_path(
        self, stub_agent_service, stub_subnet_service, stub_webhook_service
    ):
        _wire(stub_agent_service, stub_subnet_service, stub_webhook_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/subnets/subnet-1",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {
            "status": "joined",
            "agent_id": "agent-target",
            "slug": "subnet-1",
        }
        # Both service-layer mutations must have been called exactly once
        stub_agent_service.join_subnet.assert_awaited_once_with("agent-target", "subnet-1")
        stub_subnet_service.add_member.assert_awaited_once_with("subnet-1", "agent-target")

    def test_join_fires_agent_joined_webhook_when_harness_registered(
        self, stub_agent_service, stub_subnet_service, stub_webhook_service
    ):
        stub_subnet_service._default_subnet.harness_url = "https://h.example/hook"
        stub_subnet_service._default_subnet.harness_secret = "hs"

        _wire(stub_agent_service, stub_subnet_service, stub_webhook_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/subnets/subnet-1",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200
        stub_webhook_service.send_to.assert_awaited_once()
        kw = stub_webhook_service.send_to.await_args.kwargs
        assert kw["url"] == "https://h.example/hook"
        assert kw["secret"] == "hs"
        assert kw["event"] == WebhookEventType.AGENT_JOINED_SUBNET
        # ADR-0003 Phase 3 added ``parent_slug`` to the payload.
        # ``_default_subnet`` is a MagicMock stub, so the helper's
        # ``isinstance(str)`` guard returns ``None`` — matching the
        # contract for a top-level subnet.
        assert kw["data"] == {
            "slug": "subnet-1",
            "agent_id": "agent-target",
            "parent_slug": None,
        }

    def test_join_returns_403_when_path_agent_differs_from_api_key(
        self, stub_agent_service, stub_subnet_service, stub_webhook_service
    ):
        """API key for `agent-target` cannot join `agent-other` into a subnet."""
        _wire(stub_agent_service, stub_subnet_service, stub_webhook_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-other/subnets/subnet-1",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 403
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "api_key_agent_mismatch"
        # Side-effects must be guarded
        stub_agent_service.join_subnet.assert_not_awaited()
        stub_subnet_service.add_member.assert_not_awaited()

    def test_join_returns_404_for_missing_subnet(
        self, stub_agent_service, stub_subnet_service, stub_webhook_service
    ):
        _wire(stub_agent_service, stub_subnet_service, stub_webhook_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/subnets/ghost",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 404
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "subnet_not_found"
        assert body["details"] == {"slug": "ghost"}

    def test_join_returns_404_when_agent_not_found(
        self, stub_agent_service, stub_subnet_service, stub_webhook_service
    ):
        stub_agent_service.join_subnet.side_effect = AgentNotFoundException("agent-target")
        _wire(stub_agent_service, stub_subnet_service, stub_webhook_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/subnets/subnet-1",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 404
        body = r.json()
        assert body["error_code"] == "agent_not_found"

    def test_join_unauthenticated_does_not_mutate(
        self, stub_agent_service, stub_subnet_service, stub_webhook_service
    ):
        _wire(stub_agent_service, stub_subnet_service, stub_webhook_service)

        with TestClient(app) as client:
            r = client.post("/api/v1/agents/agent-target/subnets/subnet-1")

        assert 400 <= r.status_code < 500
        stub_agent_service.join_subnet.assert_not_awaited()
        stub_subnet_service.add_member.assert_not_awaited()

    def test_join_webhook_failure_does_not_500_the_response(
        self, stub_agent_service, stub_subnet_service, stub_webhook_service
    ):
        """Dead harness URL must not poison a successful join."""
        stub_subnet_service._default_subnet.harness_url = "https://dead.example"
        stub_subnet_service._default_subnet.harness_secret = "k"
        stub_webhook_service.send_to.side_effect = RuntimeError("conn refused")

        _wire(stub_agent_service, stub_subnet_service, stub_webhook_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/subnets/subnet-1",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        assert r.json()["status"] == "joined"


# ---------------------------------------------------------------------------
# DELETE /api/v1/agents/{agent_id}/subnets/{slug} — canonical leave
# ---------------------------------------------------------------------------


class TestCanonicalLeave:
    def test_leave_succeeds_under_canonical_path(
        self, stub_agent_service, stub_subnet_service, stub_webhook_service
    ):
        _wire(stub_agent_service, stub_subnet_service, stub_webhook_service)

        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/agents/agent-target/subnets/subnet-1",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        assert r.json() == {
            "status": "left",
            "agent_id": "agent-target",
            "slug": "subnet-1",
        }
        stub_agent_service.leave_subnet.assert_awaited_once_with("agent-target", "subnet-1")
        stub_subnet_service.remove_member.assert_awaited_once_with("subnet-1", "agent-target")

    def test_leave_fires_agent_left_webhook_when_harness_registered(
        self, stub_agent_service, stub_subnet_service, stub_webhook_service
    ):
        stub_subnet_service._default_subnet.harness_url = "https://h.example/hook"
        stub_subnet_service._default_subnet.harness_secret = None

        _wire(stub_agent_service, stub_subnet_service, stub_webhook_service)

        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/agents/agent-target/subnets/subnet-1",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200
        stub_webhook_service.send_to.assert_awaited_once()
        kw = stub_webhook_service.send_to.await_args.kwargs
        assert kw["event"] == WebhookEventType.AGENT_LEFT_SUBNET
        assert kw["secret"] is None  # explicit unsigned

    def test_leave_returns_403_when_path_agent_differs_from_api_key(
        self, stub_agent_service, stub_subnet_service, stub_webhook_service
    ):
        _wire(stub_agent_service, stub_subnet_service, stub_webhook_service)

        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/agents/agent-other/subnets/subnet-1",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 403
        stub_agent_service.leave_subnet.assert_not_awaited()


# ---------------------------------------------------------------------------
# GET /api/v1/agents/{agent_id}/subnets — canonical list
# ---------------------------------------------------------------------------


class TestCanonicalListAgentSubnets:
    def test_lists_own_subnet_memberships(
        self, stub_agent_service, stub_subnet_service, stub_webhook_service
    ):
        _wire(stub_agent_service, stub_subnet_service, stub_webhook_service)

        with TestClient(app) as client:
            r = client.get(
                "/api/v1/agents/agent-target/subnets",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        assert r.json() == {
            "agent_id": "agent-target",
            "subnets": ["subnet-1", "subnet-2"],
        }

    def test_returns_403_when_querying_other_agents_subnets(
        self, stub_agent_service, stub_subnet_service, stub_webhook_service
    ):
        _wire(stub_agent_service, stub_subnet_service, stub_webhook_service)

        with TestClient(app) as client:
            r = client.get(
                "/api/v1/agents/agent-other/subnets",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 403
        # Must not even reach the service layer
        stub_agent_service.get_agent.assert_not_awaited()


# ---------------------------------------------------------------------------
# Path equivalence: canonical vs legacy must be byte-for-byte identical
# ---------------------------------------------------------------------------


class TestPathEquivalence:
    """The new canonical paths and the legacy ones share `_subnet_membership.py`.

    If they ever drift — same request, different response or different
    side-effects — that's a regression in the shared helper. These tests
    are the explicit contract guard.
    """

    @pytest.mark.parametrize(
        ("verb", "canonical", "legacy"),
        [
            (
                "POST",
                "/api/v1/agents/agent-target/subnets/subnet-1",
                "/api/v1/subnets/agent-target/subnets/subnet-1",
            ),
            (
                "DELETE",
                "/api/v1/agents/agent-target/subnets/subnet-1",
                "/api/v1/subnets/agent-target/subnets/subnet-1",
            ),
            (
                "GET",
                "/api/v1/agents/agent-target/subnets",
                "/api/v1/subnets/agent-target/subnets",
            ),
        ],
    )
    def test_canonical_and_legacy_paths_return_identical_response(
        self,
        stub_agent_service,
        stub_subnet_service,
        stub_webhook_service,
        verb,
        canonical,
        legacy,
    ):
        _wire(stub_agent_service, stub_subnet_service, stub_webhook_service)

        with TestClient(app) as client:
            req = client.request
            r_canon = req(verb, canonical, headers={"Authorization": "Bearer owner-key"})
            r_legacy = req(verb, legacy, headers={"Authorization": "Bearer owner-key"})

        assert r_canon.status_code == 200, r_canon.text
        assert r_legacy.status_code == 200, r_legacy.text
        assert r_canon.json() == r_legacy.json(), (
            f"{verb} legacy vs canonical response body diverged: "
            f"{r_legacy.json()!r} != {r_canon.json()!r}"
        )

    def test_legacy_paths_are_marked_deprecated_in_openapi(self):
        """OpenAPI schema must flag every legacy agent-subnet path as deprecated.

        Catches the regression where someone refactors `routes/subnets.py` and
        accidentally drops the `deprecated=True` kwarg, defeating the whole
        "tell callers to migrate" signal.
        """
        spec = app.openapi()
        paths = spec["paths"]

        legacy_membership_paths = {
            "/api/v1/subnets/{agent_id}/subnets/{slug}": ("post", "delete"),
            "/api/v1/subnets/{agent_id}/subnets": ("get",),
        }
        for path, methods in legacy_membership_paths.items():
            assert path in paths, f"legacy path missing from OpenAPI: {path}"
            for method in methods:
                op = paths[path].get(method)
                assert op is not None, f"{method.upper()} {path} not in OpenAPI"
                assert op.get("deprecated") is True, (
                    f"{method.upper()} {path} must be marked deprecated"
                )

        canonical_membership_paths = {
            "/api/v1/agents/{agent_id}/subnets/{slug}": ("post", "delete"),
            "/api/v1/agents/{agent_id}/subnets": ("get",),
        }
        for path, methods in canonical_membership_paths.items():
            assert path in paths, f"canonical path missing from OpenAPI: {path}"
            for method in methods:
                op = paths[path].get(method)
                assert op is not None, f"{method.upper()} {path} not in OpenAPI"
                assert not op.get("deprecated"), (
                    f"{method.upper()} {path} must NOT be marked deprecated"
                )
