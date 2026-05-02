"""Tests for ``PATCH /api/v1/agents/{id}/social-card-url``.

Why a separate endpoint and a separate test file:

The SOCIAL.md pointer is conceptually distinct from registration
metadata (an owner often publishes their SOCIAL.md weeks after the
agent first joins) and we want a uniform "owner mutates one
configuration knob" shape across endpoints — same pattern as
``PATCH /policy``. Co-locating the tests here pins the contract
without dragging the policy-patch tests into a multi-knob mega
file.

We pin three categories of contract:

* **Authorization** — ``verify_owner_or_internal`` is shared with
  the policy PATCH; we sample its anonymous-rejection behavior
  here so a future regression on either endpoint is caught
  independently.
* **Validation** — empty string normalizes to ``None`` (clears
  the field), ``ftp://`` is rejected, length cap (2048) is
  enforced. The validator is duplicated across
  ``AgentRegisterRequest`` / ``AgentJoinRequest`` /
  ``SocialCardUrlPatchRequest`` because each class has a
  different surrounding shape — we cover the PATCH variant here
  and trust unit tests for the others.
* **Persistence** — successful PATCH calls
  ``AgentService.update_social_card_url`` exactly once with the
  validated payload, and the response body echoes the persisted
  URL (including ``null`` for explicit clears).

What we deliberately don't test here:
  - Fetching the URL. ACN never fetches SOCIAL.md bodies — that's
    the consumer's job per the consumption model. Asserting the
    no-fetch behavior would be testing absence-of-side-effect on
    every code path; the simpler invariant is "ACN's storage
    layer holds the pointer, full stop", which is covered by the
    persistence assertion plus the fact that there's no httpx
    import in the route code.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import AgentNotFoundException
from acn.routes.dependencies import (
    _api_key_cache,
    get_agent_service,
    limiter,
)

VALID_INTERNAL_TOKEN = "test-internal-token-min-32-chars-padding"


@pytest.fixture(autouse=True)
def _reset_state():
    limiter.enabled = False
    _api_key_cache.clear()
    yield
    limiter.enabled = True
    _api_key_cache.clear()
    app.dependency_overrides.clear()


@pytest.fixture
def stub_agent_service():
    """Same ``get_agent_by_api_key`` shape as the policy-patch
    tests so the two suites can evolve together; the
    ``update_social_card_url`` mock echoes the input back into a
    MagicMock so tests can assert the persisted value."""
    svc = AsyncMock()

    target = MagicMock()
    target.agent_id = "agent-target"
    target.name = "Target"

    other = MagicMock()
    other.agent_id = "agent-other"
    other.name = "Other"

    async def _by_api_key(key: str):
        if key == "owner-key":
            return target
        if key == "other-key":
            return other
        return None

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)

    async def _update(agent_id: str, social_card_url):
        if agent_id != "agent-target":
            raise AgentNotFoundException(agent_id)
        result = MagicMock()
        result.agent_id = agent_id
        result.social_card_url = social_card_url
        return result

    svc.update_social_card_url = AsyncMock(side_effect=_update)

    return svc


def _wire(svc) -> None:
    app.dependency_overrides[get_agent_service] = lambda: svc


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #


class TestAuth:
    def test_anonymous_rejected(self, stub_agent_service):
        """Same auth gate as PATCH /policy — anonymous callers
        must NEVER be able to swap an agent's published SOCIAL.md
        URL. A successful anonymous PATCH would let an attacker
        redirect every counterparty to a phishing card."""
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/social-card-url",
                json={"social_card_url": "https://evil.example.com/social.md"},
            )

        assert r.status_code == 401, r.text
        stub_agent_service.update_social_card_url.assert_not_awaited()

    def test_other_agent_cannot_patch(self, stub_agent_service):
        """Cross-tenant boundary mirrors policy PATCH: an API key
        for X must not let X redirect Y's social profile."""
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/social-card-url",
                json={"social_card_url": "https://evil.example.com/social.md"},
                headers={"Authorization": "Bearer other-key"},
            )

        assert r.status_code == 403, r.text
        stub_agent_service.update_social_card_url.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


class TestValidation:
    def test_non_http_scheme_rejected(self, stub_agent_service):
        """Why we restrict to http(s): the URL is meant to be
        fetched by HTTP clients per the consumption model.
        Allowing arbitrary schemes (``ftp://``, ``file://``,
        ``javascript:``) would (a) break consumers, (b) create an
        SSRF / XSS vector if any consumer naively dereferences."""
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/social-card-url",
                json={"social_card_url": "ftp://example.com/social.md"},
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 422, r.text
        assert "https://" in r.text
        stub_agent_service.update_social_card_url.assert_not_awaited()

    def test_empty_string_normalizes_to_null(self, stub_agent_service):
        """Empty-string input is treated as a clear, not as a
        validation error. Rationale: most form UIs send ``""``
        when an input is blanked rather than dropping the key
        entirely. Forcing the client to send literal ``null``
        would create a frontend foot-gun."""
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/social-card-url",
                json={"social_card_url": "   "},
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        assert r.json()["social_card_url"] is None
        stub_agent_service.update_social_card_url.assert_awaited_once_with(
            agent_id="agent-target",
            social_card_url=None,
        )

    def test_oversized_url_rejected(self, stub_agent_service):
        """The 2048-char cap matches the de-facto browser URL
        limit. Without it, an owner could store an arbitrarily
        large blob (turning the column into a side-channel for
        free storage), and consumers would ship pathologically
        long requests trying to fetch it."""
        _wire(stub_agent_service)

        oversize = "https://example.com/" + ("a" * 3000)
        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/social-card-url",
                json={"social_card_url": oversize},
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 422, r.text
        stub_agent_service.update_social_card_url.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Persistence flow
# --------------------------------------------------------------------------- #


class TestPersistence:
    def test_owner_can_set_url(self, stub_agent_service):
        """Core happy path: owner publishes their SOCIAL.md
        pointer and gets back the persisted shape. We assert on
        the service call (not just the response) to confirm we
        actually hit the repository."""
        _wire(stub_agent_service)

        url = "https://acme.example.com/.well-known/social.md"
        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/social-card-url",
                json={"social_card_url": url},
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        assert r.json() == {
            "agent_id": "agent-target",
            "social_card_url": url,
        }
        stub_agent_service.update_social_card_url.assert_awaited_once_with(
            agent_id="agent-target",
            social_card_url=url,
        )

    def test_null_clears_url(self, stub_agent_service):
        """Explicit ``null`` clears the field. Useful when an
        owner deprecates their SOCIAL.md (e.g. moving to a fully
        closed mode where they don't want any social pointer
        published at all)."""
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/social-card-url",
                json={"social_card_url": None},
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        assert r.json() == {
            "agent_id": "agent-target",
            "social_card_url": None,
        }
        stub_agent_service.update_social_card_url.assert_awaited_once_with(
            agent_id="agent-target",
            social_card_url=None,
        )

    def test_internal_token_can_force_clear(self, stub_agent_service):
        """Ops scenario: an agent's owner domain has been hijacked
        and is now serving a malicious SOCIAL.md. The internal
        token branch must reach the same service method as the
        owner branch — same code path, same audit shape, no
        separate ops-only escape hatch that could drift."""
        _wire(stub_agent_service)

        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ):
            with TestClient(app) as client:
                r = client.patch(
                    "/api/v1/agents/agent-target/social-card-url",
                    json={"social_card_url": None},
                    headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
                )

        assert r.status_code == 200, r.text
        stub_agent_service.update_social_card_url.assert_awaited_once_with(
            agent_id="agent-target",
            social_card_url=None,
        )

    def test_unknown_agent_returns_404(self, stub_agent_service):
        """Auth precedes existence (anonymous test above proves
        it). Once authenticated, surfacing 404 is fine — the
        gating layer for ``does this agent ID exist`` is the
        registration endpoint, not this one."""
        _wire(stub_agent_service)

        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ):
            with TestClient(app) as client:
                r = client.patch(
                    "/api/v1/agents/does-not-exist/social-card-url",
                    json={"social_card_url": "https://example.com/social.md"},
                    headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
                )

        assert r.status_code == 404, r.text


# --------------------------------------------------------------------------- #
# Cross-entry-point validation parity
# --------------------------------------------------------------------------- #


class TestCrossEntryPointValidation:
    """``AgentRegisterRequest`` (POST /register), ``AgentJoinRequest``
    (POST /join), and ``SocialCardUrlPatchRequest`` (PATCH
    /social-card-url) each have their own ``social_card_url``
    field validator (intentionally — they live in different
    Pydantic classes with different surrounding shapes). This
    test pins that the three validators share the same accept /
    reject set, so a future maintainer "fixing" one validator
    can't accidentally let an unsupported scheme through another
    entry point.
    """

    @pytest.mark.parametrize(
        "value,expected_to_pass",
        [
            ("https://acme.example.com/.well-known/social.md", True),
            ("http://localhost:3000/social.md", True),  # dev mode
            ("   ", True),  # normalized to None
            (None, True),
            ("ftp://example.com/social.md", False),
            ("javascript:alert(1)", False),
            ("", True),  # normalized to None
            ("not-a-url", False),
        ],
    )
    def test_three_entry_points_agree(self, value, expected_to_pass):
        from pydantic import ValidationError

        from acn.models import AgentRegisterRequest
        from acn.routes.registry import AgentJoinRequest, SocialCardUrlPatchRequest

        register_kwargs = {
            "owner": "user-1",
            "name": "Test",
            "endpoint": "https://example.com/a2a",
            "social_card_url": value,
        }
        join_kwargs = {
            "name": "TestAgent",
            "description": "A test agent that does things",
            "endpoint": "https://example.com/a2a",
            "social_card_url": value,
        }
        patch_kwargs = {"social_card_url": value}

        if expected_to_pass:
            r1 = AgentRegisterRequest(**register_kwargs)
            r2 = AgentJoinRequest(**join_kwargs)
            r3 = SocialCardUrlPatchRequest(**patch_kwargs)
            # Empty / whitespace must collapse to None across all
            # three so downstream service code only ever sees
            # ``None`` or a real URL.
            if value is None or (isinstance(value, str) and not value.strip()):
                assert r1.social_card_url is None
                assert r2.social_card_url is None
                assert r3.social_card_url is None
        else:
            with pytest.raises(ValidationError):
                AgentRegisterRequest(**register_kwargs)
            with pytest.raises(ValidationError):
                AgentJoinRequest(**join_kwargs)
            with pytest.raises(ValidationError):
                SocialCardUrlPatchRequest(**patch_kwargs)
