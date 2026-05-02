"""Follows routes — flat ACN error schema contract tests.

Phase 2 review v2 P1 #11 sprint row #6 — pin the 5 4xx sites in
``acn/routes/follows.py`` to the canonical ``ACNHTTPError`` flat
schema after their migration from raw ``HTTPException``.

This file complements ``tests/services/test_follow_service.py``
(domain logic) and any route-level behaviour tests by asserting
only the *response shape* — the four-field contract SDK clients
depend on:

* ``error_code``  — stable ASCII branch key
* ``message``     — human-readable prose, never to be string-matched
* ``details``     — code-specific structured context, keys
                    documented in ``docs/features/acn-error-schema.md``
* ``request_id``  — UUID echoed in the ``X-Request-ID`` response header

We also assert ``"detail"`` is **absent** from migrated responses —
its presence would indicate a leak of legacy ``HTTPException``
shape and SDK clients have an explicit branch that would
mis-route those.

Coverage matrix
---------------
5 4xx sites × 4 distinct error codes:

* ``API_KEY_AGENT_MISMATCH`` (×2 — `follow_agent` POST and
  `unfollow_agent` DELETE both gate on ``caller["agent_id"] !=
  agent_id``). Each verb is exercised independently — a refactor
  that breaks the gate on one verb without breaking the other
  would otherwise hide.
* ``SELF_FOLLOW_FORBIDDEN`` (×1 — POST self-follow attempt).
* ``AGENT_NOT_FOUND`` (×1 — POST against a missing followee).
* ``FOLLOW_LIMIT_EXCEEDED`` (×1 — POST when follower at
  ``MAX_FOLLOWS``). The 429 status is per ``acn-follow-proposal.md``
  ("超出返回 429"); ``details.max_follows`` lets clients pre-flight
  on retry without hardcoding the constant.

Naming choice — ``follower_id`` vs ``owner_id``
-----------------------------------------------
sprint #1 (allowlist) used ``owner_id`` for the corresponding
``self_allowlist_forbidden`` and ``allowlist_capacity_exceeded``
codes because the allowlist is *owned* by the agent. follow has
no ownership semantics — the operating entity is a *follower*, and
both the service-layer exception names
(``FollowLimitExceededError``, ``SelfFollowError``) and the
``acn-follow-proposal.md`` response bodies use ``follower``. So
``details.follower_id`` here, not ``details.owner_id``. The two
codes are semantically parallel but field-name divergent on
purpose.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import AgentNotFoundException
from acn.routes.dependencies import (
    get_follow_service,
    verify_agent_api_key,
)
from acn.services import FollowLimitExceededError, SelfFollowError
from acn.services.follow_service import MAX_FOLLOWS
from tests.routes.conftest import _assert_flat_shape


def _agent_info(agent_id: str = "agent-self") -> dict:
    """Match the dict shape returned by ``verify_agent_api_key``."""
    return {"agent_id": agent_id, "owner": "user-1"}


@pytest.fixture
def stub_follow_service():
    svc = AsyncMock()
    svc.follow = AsyncMock(return_value=True)
    svc.unfollow = AsyncMock(return_value=True)
    return svc


def _wire(follow_svc, agent_id: str = "agent-self") -> None:
    """Override the follow service and the auth dependency."""
    app.dependency_overrides[get_follow_service] = lambda: follow_svc
    app.dependency_overrides[verify_agent_api_key] = lambda: _agent_info(agent_id)


# ============================================================================
# API_KEY_AGENT_MISMATCH (2 sites — POST + DELETE)
# ============================================================================


class TestApiKeyAgentMismatchFlatShape:
    """Both follow / unfollow gate on ``caller["agent_id"] != agent_id``;
    the gates live in distinct handlers so each verb is pinned."""

    EXPECTED_DETAILS = {
        "path_agent": "agent-target",
        "key_agent": "agent-other",
    }

    def test_follow_403_flat_shape(self, stub_follow_service):
        _wire(stub_follow_service, agent_id="agent-other")
        with TestClient(app) as client:
            r = client.post("/api/v1/agents/agent-target/follows/agent-x")
        assert r.status_code == 403
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "api_key_agent_mismatch"
        assert body["details"] == self.EXPECTED_DETAILS
        assert r.headers.get("X-Request-ID") == body["request_id"]

    def test_unfollow_403_flat_shape(self, stub_follow_service):
        _wire(stub_follow_service, agent_id="agent-other")
        with TestClient(app) as client:
            r = client.delete("/api/v1/agents/agent-target/follows/agent-x")
        assert r.status_code == 403
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "api_key_agent_mismatch"
        assert body["details"] == self.EXPECTED_DETAILS


# ============================================================================
# SELF_FOLLOW_FORBIDDEN (1 site)
# ============================================================================


class TestSelfFollowForbiddenFlatShape:
    """POST self-follow → 400 with ``error_code=self_follow_forbidden``
    and ``{follower_id}``. Mirrors sprint #1's ``self_allowlist_forbidden``
    (which uses ``owner_id``) but uses ``follower_id`` because follow
    has no ownership semantics."""

    def test_self_follow_400_flat_shape(self, stub_follow_service):
        stub_follow_service.follow = AsyncMock(side_effect=SelfFollowError("self"))
        _wire(stub_follow_service, agent_id="agent-self")
        with TestClient(app) as client:
            r = client.post("/api/v1/agents/agent-self/follows/agent-self")
        assert r.status_code == 400
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "self_follow_forbidden"
        assert body["details"] == {"follower_id": "agent-self"}


# ============================================================================
# AGENT_NOT_FOUND (1 site — followee missing)
# ============================================================================


class TestAgentNotFoundFlatShape:
    """POST against a missing followee → 404 with the *target* in
    ``details.agent_id`` (matches sprint #1 allowlist semantics: the
    missing entity is the target, not the follower)."""

    def test_followee_missing_404_flat_shape(self, stub_follow_service):
        stub_follow_service.follow = AsyncMock(
            side_effect=AgentNotFoundException("Agent ghost not found")
        )
        _wire(stub_follow_service, agent_id="agent-self")
        with TestClient(app) as client:
            r = client.post("/api/v1/agents/agent-self/follows/agent-ghost")
        assert r.status_code == 404
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "agent_not_found"
        assert body["details"] == {"agent_id": "agent-ghost"}


# ============================================================================
# FOLLOW_LIMIT_EXCEEDED (1 site)
# ============================================================================


class TestFollowLimitExceededFlatShape:
    """POST when follower at ``MAX_FOLLOWS`` → 429 with
    ``error_code=follow_limit_exceeded`` and
    ``{follower_id, max_follows}``. ``max_follows`` is the documented
    contract knob — clients can pre-flight from this on retry rather
    than hardcoding the constant. Mirrors sprint #1's
    ``allowlist_capacity_exceeded`` but uses ``follower_id`` /
    ``max_follows`` (not ``owner_id`` / ``max_size``) because follow
    has no ownership semantics."""

    def test_429_flat_shape(self, stub_follow_service):
        stub_follow_service.follow = AsyncMock(
            side_effect=FollowLimitExceededError("limit reached")
        )
        _wire(stub_follow_service, agent_id="agent-self")
        with TestClient(app) as client:
            r = client.post("/api/v1/agents/agent-self/follows/agent-x")
        assert r.status_code == 429
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "follow_limit_exceeded"
        assert body["details"] == {
            "follower_id": "agent-self",
            "max_follows": MAX_FOLLOWS,
        }
