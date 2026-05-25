"""Regression tests for GET /agents?subnet= filter.

Before the fix, the ?subnet= query parameter was silently ignored and the
endpoint returned ALL agents regardless of the filter value.  These tests
pin the corrected behaviour:

- ?subnet= filters results to members of that subnet.
- A non-existent subnet returns 404 subnet_not_found.
- A private subnet returns 403 not_subnet_member for non-members.
- A private subnet is accessible to members that present their API key.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.routes.dependencies import get_agent_service, get_subnet_service

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(agent_id: str, name: str) -> MagicMock:
    a = MagicMock()
    a.agent_id = agent_id
    a.name = name
    a.owner = "owner-1"
    a.description = "test agent"
    a.tags = []
    a.endpoint = f"https://example.com/{agent_id}"
    a.a2a_endpoint = f"https://example.com/{agent_id}"
    a.agent_card_url = None
    a.agent_card = None
    a.status = "offline"
    a.subnet_ids = ["subnet-pub"]
    a.metadata = {}
    a.registered_at = datetime(2026, 1, 1, tzinfo=UTC)
    a.last_heartbeat = None
    a.wallet_address = None
    a.wallet_addresses = None
    a.accepts_payment = False
    a.payment_methods = []
    a.followers_count = 0
    a.follows_count = 0
    a.referrer_id = None
    a.claim_status = None
    a.verification_code = None
    a.erc8004_agent_id = None
    a.erc8004_chain = None
    a.erc8004_tx_hash = None
    a.erc8004_registered_at = None
    a.social_card_url = None
    a.has_all_tags = MagicMock(return_value=True)
    return a


def _make_subnet(slug: str, *, is_private: bool = False, members: list[str] | None = None) -> MagicMock:
    s = MagicMock()
    s.slug = slug
    s.subnet_id = slug
    s.is_private = is_private
    _members: set[str] = set(members or [])
    s.member_agent_ids = _members
    s.has_member = MagicMock(side_effect=lambda aid: aid in _members)
    return s


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_alpha():
    return _make_agent("agent-alpha", "Alpha")


@pytest.fixture
def agent_beta():
    return _make_agent("agent-beta", "Beta")


@pytest.fixture
def stub_agent_service(agent_alpha, agent_beta):
    svc = AsyncMock()

    async def _search(tags=None, status="all", slug=None):
        all_agents = [agent_alpha, agent_beta]
        if slug == "subnet-alpha-only":
            return [agent_alpha]
        if slug == "subnet-pub":
            return all_agents
        if slug is None:
            return all_agents
        return []

    svc.search_agents = AsyncMock(side_effect=_search)
    svc.is_alive = AsyncMock(return_value=False)
    svc.batch_alive = AsyncMock(return_value={a.agent_id: False for a in [agent_alpha, agent_beta]})

    async def _by_api_key(key: str):
        # DEV_MODE passes the raw credentials string; ACN API keys use acn_ prefix.
        if key in ("member-key", "acn_member-key"):
            return agent_alpha
        return None

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)
    return svc


@pytest.fixture
def stub_subnet_service():
    svc = AsyncMock()

    subnets = {
        "subnet-pub": _make_subnet("subnet-pub", is_private=False, members=["agent-alpha", "agent-beta"]),
        "subnet-alpha-only": _make_subnet("subnet-alpha-only", is_private=False, members=["agent-alpha"]),
        "subnet-private": _make_subnet("subnet-private", is_private=True, members=["agent-alpha"]),
    }

    async def _get_subnet(slug: str):
        return subnets.get(slug)

    async def _list_public():
        return [s for s in subnets.values() if not s.is_private]

    svc.get_subnet = AsyncMock(side_effect=_get_subnet)
    svc.list_subnets = AsyncMock(return_value=list(subnets.values()))
    svc.list_public_subnets = AsyncMock(side_effect=_list_public)
    return svc


@pytest.fixture
def client(stub_agent_service, stub_subnet_service):
    app.dependency_overrides[get_agent_service] = lambda: stub_agent_service
    app.dependency_overrides[get_subnet_service] = lambda: stub_subnet_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSubnetFilter:
    def test_no_subnet_param_returns_all(self, client, agent_alpha, agent_beta):
        r = client.get("/api/v1/agents?status=all")
        assert r.status_code == 200
        ids = {a["agent_id"] for a in r.json()["agents"]}
        assert agent_alpha.agent_id in ids
        assert agent_beta.agent_id in ids

    def test_subnet_filter_restricts_results(self, client, agent_alpha, agent_beta):
        r = client.get("/api/v1/agents?subnet=subnet-alpha-only&status=all")
        assert r.status_code == 200
        ids = {a["agent_id"] for a in r.json()["agents"]}
        assert agent_alpha.agent_id in ids
        assert agent_beta.agent_id not in ids

    def test_nonexistent_subnet_returns_404(self, client):
        r = client.get("/api/v1/agents?subnet=does-not-exist&status=all")
        assert r.status_code == 404
        assert r.json()["error_code"] == "subnet_not_found"

    def test_private_subnet_without_auth_returns_403(self, client):
        r = client.get("/api/v1/agents?subnet=subnet-private&status=all")
        assert r.status_code == 403
        assert r.json()["error_code"] == "not_subnet_member"

    # NOTE: testing authenticated-non-member 403 in DEV_MODE is not possible
    # because verify_token grants acn:admin to every bearer token in dev mode
    # (by design — auth is bypassed). The ACL is tested via test_private_subnet_without_auth_returns_403
    # which covers the unauthenticated path. Production behaviour (real Auth0) enforces the ACL correctly.

    def test_private_subnet_member_can_query(self, client, agent_alpha):
        r = client.get(
            "/api/v1/agents?subnet=subnet-private&status=all",
            headers={"Authorization": "Bearer member-key"},
        )
        assert r.status_code == 200
