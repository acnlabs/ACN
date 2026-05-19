"""Route-level contract tests for ADR-0004 Phase 1 — ``join_policy`` on
``POST /api/v1/subnets``.

Pins the API surface for the new field:

1. Legacy body (``is_private=true``, no ``join_policy``) succeeds —
   the service infers ``'approval'`` server-side and the response echoes
   that effective value. **This is the most critical regression case**:
   without server-side inference, every existing client that just
   flips ``is_private=true`` would 500 on the entity-layer invariant.
2. Legacy body for public subnets defaults to ``'open'``.
3. Explicit ``join_policy='approval'`` on a public subnet is accepted
   (curated community board, one of the three ADR-permitted combinations).
4. Explicit ``join_policy='open'`` on a private subnet is rejected
   with ``INVALID_REQUEST 400`` and ``details.reason =
   "visibility_policy_conflict"`` — the stable token CLI / SDK
   parsers pin against.
5. ``SubnetCreateResponse.join_policy`` is always populated (mirrors
   the service-side resolution so callers don't have to guess what
   the inference rule produced).

Mirrors the harness used by ``test_subnets_create_membership.py`` —
``subnet_service.create_subnet`` is stubbed end-to-end so these are
contract tests, not integration. The actual server-side inference
behaviour is pinned in ``tests/services/test_subnet_service.py``
under ``TestSubnetServiceADR0004JoinPolicy``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.routes.dependencies import (
    get_agent_service,
    get_subnet_service,
)
from acn.services.subnet_service import (
    REASON_VISIBILITY_POLICY_CONFLICT,
    SubnetNestingError,
)


@pytest.fixture
def stub_agent_service():
    svc = AsyncMock()
    target = MagicMock()
    target.agent_id = "agent-target"
    target.name = "Target"
    target.subnet_ids = []

    async def _by_api_key(key: str):
        return {"owner-key": target}.get(key)

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)
    svc.get_agent = AsyncMock(return_value=target)
    svc.join_subnet = AsyncMock(return_value=None)
    return svc


def _make_subnet_mock(
    subnet_id: str,
    *,
    owner: str = "agent-target",
    is_private: bool = False,
    join_policy: str = "open",
):
    sn = MagicMock()
    sn.subnet_id = subnet_id
    sn.owner = owner
    sn.is_private = is_private
    sn.harness_url = None
    sn.harness_secret = None
    sn.member_agent_ids = {owner}
    sn.join_policy = join_policy
    return sn


@pytest.fixture
def stub_subnet_service():
    """``create_subnet`` echoes the inference rule the real service
    applies — so the route test exercises the *response-shape*
    contract end-to-end without re-implementing the service."""
    svc = AsyncMock()

    async def _create_subnet(**kwargs: Any):
        is_private = kwargs.get("is_private", False)
        join_policy = kwargs.get("join_policy")
        if join_policy is None:
            effective = "approval" if is_private else "open"
        else:
            if is_private and join_policy == "open":
                raise SubnetNestingError(
                    REASON_VISIBILITY_POLICY_CONFLICT,
                    "is_private=True requires join_policy='approval'",
                )
            effective = join_policy
        return _make_subnet_mock(
            kwargs["subnet_id"],
            owner=kwargs["owner"],
            is_private=is_private,
            join_policy=effective,
        )

    svc.create_subnet = AsyncMock(side_effect=_create_subnet)
    svc.delete_subnet = AsyncMock(return_value=True)
    return svc


def _wire(agent_svc, subnet_svc) -> None:
    app.dependency_overrides[get_agent_service] = lambda: agent_svc
    app.dependency_overrides[get_subnet_service] = lambda: subnet_svc


class TestJoinPolicyInference:
    def test_legacy_private_body_auto_upgrades_to_approval(
        self, stub_agent_service, stub_subnet_service
    ):
        """**Critical regression target.** Existing clients send
        ``POST /api/v1/subnets`` with ``"is_private": true`` and no
        ``join_policy``. Without server-side inference every such
        call would 500 on the entity invariant. The response must
        echo ``join_policy='approval'`` so the client can observe
        what the server chose."""
        _wire(stub_agent_service, stub_subnet_service)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/subnets",
                headers={"Authorization": "Bearer owner-key"},
                json={
                    "name": "Legacy Private",
                    "subnet_id": "subnet-legacy-priv",
                    "is_private": True,
                },
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["is_public"] is False
        assert body["join_policy"] == "approval"
        call_kwargs = stub_subnet_service.create_subnet.await_args.kwargs
        assert call_kwargs["join_policy"] is None
        assert call_kwargs["is_private"] is True

    def test_legacy_public_body_defaults_to_open(
        self, stub_agent_service, stub_subnet_service
    ):
        _wire(stub_agent_service, stub_subnet_service)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/subnets",
                headers={"Authorization": "Bearer owner-key"},
                json={
                    "name": "Legacy Public",
                    "subnet_id": "subnet-legacy-pub",
                },
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["is_public"] is True
        assert body["join_policy"] == "open"


class TestExplicitJoinPolicy:
    def test_public_plus_approval_accepted(
        self, stub_agent_service, stub_subnet_service
    ):
        """Curated community board: ``is_private=false`` +
        ``join_policy='approval'`` is one of the three ADR-permitted
        combinations."""
        _wire(stub_agent_service, stub_subnet_service)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/subnets",
                headers={"Authorization": "Bearer owner-key"},
                json={
                    "name": "Curated Board",
                    "subnet_id": "subnet-curated",
                    "is_private": False,
                    "join_policy": "approval",
                },
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["is_public"] is True
        assert body["join_policy"] == "approval"

    def test_private_plus_approval_accepted_explicit(
        self, stub_agent_service, stub_subnet_service
    ):
        _wire(stub_agent_service, stub_subnet_service)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/subnets",
                headers={"Authorization": "Bearer owner-key"},
                json={
                    "name": "Forward-Aware Private",
                    "subnet_id": "subnet-aware-priv",
                    "is_private": True,
                    "join_policy": "approval",
                },
            )

        assert r.status_code == 200, r.text
        assert r.json()["join_policy"] == "approval"

    def test_private_plus_open_rejected_with_stable_reason(
        self, stub_agent_service, stub_subnet_service
    ):
        """The only rejected combination. Must surface as
        ``INVALID_REQUEST 400`` with
        ``details.reason='visibility_policy_conflict'`` — the stable
        token CLI / SDK parsers pin against. Without this contract,
        clients have to scrape free-form English to recognise the
        conflict."""
        _wire(stub_agent_service, stub_subnet_service)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/subnets",
                headers={"Authorization": "Bearer owner-key"},
                json={
                    "name": "Conflict",
                    "subnet_id": "subnet-conflict",
                    "is_private": True,
                    "join_policy": "open",
                },
            )

        assert r.status_code == 400, r.text
        payload = r.json()
        assert payload["error_code"] == "invalid_request"
        assert payload["details"]["reason"] == REASON_VISIBILITY_POLICY_CONFLICT
        stub_agent_service.join_subnet.assert_not_awaited()


class TestRequestModelValidation:
    """``SubnetCreateRequest.join_policy`` is typed
    ``Literal["open", "approval"] | None``. FastAPI's request
    validation rejects anything outside the literal set before the
    route body runs; the rejection is surfaced through ACN's
    canonical error envelope (``INVALID_REQUEST 400``), same as the
    rest of the API."""

    def test_unknown_join_policy_value_rejected_at_request_validation(
        self, stub_agent_service, stub_subnet_service
    ):
        _wire(stub_agent_service, stub_subnet_service)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/subnets",
                headers={"Authorization": "Bearer owner-key"},
                json={
                    "name": "Bad",
                    "subnet_id": "subnet-bad",
                    "join_policy": "moderated",  # not in the literal set
                },
            )

        # FastAPI surfaces RequestValidationError through ACN's
        # ``INVALID_REQUEST 400`` envelope (see ``acn/api.py``
        # exception handler). Either 400 or 422 is acceptable —
        # we pin "must be 4xx and service must not have been
        # invoked", since the exact code depends on which handler
        # catches it first.
        assert 400 <= r.status_code < 500, r.text
        stub_subnet_service.create_subnet.assert_not_awaited()
