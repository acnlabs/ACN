"""Pin the ``claim_url`` string contract returned by ``POST /agents/join``.

This file exists because the previous shape — ``/claim/{id}/{token}``
with the token as a path segment — silently 404'd in production: the
frontend page lives at ``app/claim/[id]/page.tsx`` and reads the token
from ``searchParams.get("token")``. The dynamic route has a single
``[id]`` segment, so the token-as-path-segment form never matched any
Next.js route — every human onboarding link was broken for as long as
the form mismatch existed.

We only had **zero** tests covering ``claim_url`` before this file —
that's why the mismatch survived to production. These tests pin the
two invariants any future refactor must preserve:

1. **Query-string shape.** ``claim_url`` MUST be
   ``<frontend>/claim/<agent_id>?token=<encoded_token>`` — the path
   form is forbidden because it cannot be served by the frontend.
2. **URL-safe encoding of the token.** The current implementation uses
   ``urllib.parse.quote(token, safe="")``. ``secrets.token_urlsafe``
   only emits ``[A-Za-z0-9_-]``, so encoding is a no-op today; the
   test pins the behaviour for tokens that DO contain reserved
   characters, so a future alphabet change can't reintroduce the bug
   silently.

We test ``_join_agent_impl`` directly rather than going through the
FastAPI route because the route adds rate-limit / SlowAPI / SSRF /
background-task plumbing that has nothing to do with this string
contract — adding all of it would dilute the test's signal.

``_resolve_registration_endpoint`` is mocked here because it performs
real DNS lookups and HTTP probes; those behaviours are tested separately
in ``test_endpoint_reachability.py``. This file cares only about the
``claim_url`` shape after a successful endpoint resolution.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks

from acn.monitoring.audit import AuditEventType
from acn.routes.registry import AgentJoinRequest, _agent_entity_to_info, _join_agent_impl


@pytest.fixture
def fake_agent():
    """A minimal Agent stub matching the attribute surface that
    ``_join_agent_impl`` reads after ``join_agent`` returns."""
    agent = MagicMock()
    agent.agent_id = "agent-probe-abc123"
    agent.name = "ProbeAgent"
    agent.status = MagicMock(value="active")
    agent.claim_status = MagicMock(value="unclaimed")
    # Default to a realistic token_urlsafe output (URL-safe alphabet)
    agent.verification_code = "QjCbj7z_O4EgAwUcHNMbwSSUbjgrnX9ZENehhlYukds"
    # Set a concrete policy so the join response can echo a real mode
    # string. MagicMock's default attribute would otherwise be another
    # MagicMock and Pydantic would reject it.
    agent.communication_policy = {"mode": "open"}
    return agent


@pytest.fixture
def stub_join_service(fake_agent):
    """Stub ``agent_service`` with the single async method
    ``_join_agent_impl`` calls — ``join_agent`` returning
    ``(agent, api_key)``."""
    svc = AsyncMock()
    svc.join_agent = AsyncMock(return_value=(fake_agent, "acn_test_api_key"))
    return svc


def _make_body() -> AgentJoinRequest:
    return AgentJoinRequest(
        name="ProbeAgent",
        description="Pin claim_url shape; does not register against real ACN.",
        tags=["probe"],
        a2a_endpoint="https://probe.example.com/a2a",
    )


@pytest.mark.asyncio
async def test_claim_url_uses_query_token_form(stub_join_service, fake_agent):
    """``claim_url`` MUST be ``…/claim/<agent_id>?token=<token>`` —
    the old ``…/claim/<agent_id>/<token>`` path-segment form is what
    silently 404'd in production. Any future change that reintroduces
    the path form must fail this test."""
    with patch(
        "acn.routes.registry._resolve_registration_endpoint",
        new=AsyncMock(return_value=("https://probe.example.com/a2a", None, True, True)),
    ):
        resp = await _join_agent_impl(
            _make_body(),
            BackgroundTasks(),
            ref=None,
            agent_service=stub_join_service,
        )

    aid = fake_agent.agent_id
    token = fake_agent.verification_code

    # Positive: query form present, exact pattern.
    assert f"/claim/{aid}?token={token}" in resp.claim_url, (
        f"Expected ?token= query form in claim_url; got {resp.claim_url!r}"
    )

    # Negative: the old path-segment form must not appear anywhere.
    assert f"/claim/{aid}/{token}" not in resp.claim_url, (
        "Path-segment form /claim/<id>/<token> reintroduced — the frontend "
        f"route has only a single [id] segment and 404s on this; got {resp.claim_url!r}"
    )


@pytest.mark.asyncio
async def test_claim_url_url_encodes_reserved_characters(stub_join_service, fake_agent):
    """If ``verification_code`` ever ships in an alphabet wider than
    ``token_urlsafe``'s ``[A-Za-z0-9_-]``, the encoding MUST kick in —
    otherwise a token like ``a&b=c`` would split into bogus query
    params on the frontend side and the claim would silently fail.
    We force a token full of reserved characters and assert each one
    survives as its percent-encoded form."""
    fake_agent.verification_code = "with/slash&amp=eq?qmark#hash"

    with patch(
        "acn.routes.registry._resolve_registration_endpoint",
        new=AsyncMock(return_value=("https://probe.example.com/a2a", None, True, True)),
    ):
        resp = await _join_agent_impl(
            _make_body(),
            BackgroundTasks(),
            ref=None,
            agent_service=stub_join_service,
        )

    # Every reserved char must come out percent-encoded so the
    # query string parses to a single ``token`` param value.
    encoded = "with%2Fslash%26amp%3Deq%3Fqmark%23hash"
    assert encoded in resp.claim_url, (
        f"Reserved characters not percent-encoded; got {resp.claim_url!r}"
    )
    # And the raw form must NOT appear — that would mean we forgot to
    # call quote() somewhere on the path.
    assert "with/slash&amp=eq?qmark#hash" not in resp.claim_url


@pytest.mark.asyncio
async def test_claim_url_uses_interfaze_origin_when_set(stub_join_service, fake_agent):
    """``INTERFAZE_BASE_URL`` wins for claim_url; path stays ``/claim/<id>?token=``."""
    from acn.routes import registry as registry_mod

    previous = registry_mod.settings.interfaze_base_url
    registry_mod.settings.interfaze_base_url = "https://interfaze.io"
    try:
        with patch(
            "acn.routes.registry._resolve_registration_endpoint",
            new=AsyncMock(return_value=("https://probe.example.com/a2a", None, True, True)),
        ):
            resp = await _join_agent_impl(
                _make_body(),
                BackgroundTasks(),
                ref=None,
                agent_service=stub_join_service,
            )
    finally:
        registry_mod.settings.interfaze_base_url = previous

    aid = fake_agent.agent_id
    token = fake_agent.verification_code
    assert resp.claim_url.startswith("https://interfaze.io/claim/")
    assert f"/claim/{aid}?token={token}" in resp.claim_url


@pytest.mark.asyncio
async def test_claim_url_falls_back_without_interfaze_origin(stub_join_service, fake_agent):
    """Unset Interfaze origin keeps AgentPlanet / Labs FRONTEND_BASE_URL."""
    from acn.routes import registry as registry_mod

    previous_iz = registry_mod.settings.interfaze_base_url
    previous_fe = registry_mod.settings.frontend_base_url
    registry_mod.settings.interfaze_base_url = None
    registry_mod.settings.frontend_base_url = "https://agentplanet.example"
    try:
        with patch(
            "acn.routes.registry._resolve_registration_endpoint",
            new=AsyncMock(return_value=("https://probe.example.com/a2a", None, True, True)),
        ):
            resp = await _join_agent_impl(
                _make_body(),
                BackgroundTasks(),
                ref=None,
                agent_service=stub_join_service,
            )
    finally:
        registry_mod.settings.interfaze_base_url = previous_iz
        registry_mod.settings.frontend_base_url = previous_fe

    assert resp.claim_url.startswith("https://agentplanet.example/claim/")


@pytest.mark.asyncio
async def test_join_stores_host_invite_code_only(stub_join_service, fake_agent):
    """``invite=ji_…`` lands in metadata.join_invite; owner subs are dropped."""
    with patch(
        "acn.routes.registry._resolve_registration_endpoint",
        new=AsyncMock(return_value=("https://probe.example.com/a2a", None, True, True)),
    ):
        await _join_agent_impl(
            _make_body(),
            BackgroundTasks(),
            ref=None,
            agent_service=stub_join_service,
            invite="ji_shareableCode_01",
        )

    kwargs = stub_join_service.join_agent.await_args.kwargs
    assert kwargs["metadata"]["join_invite"] == "ji_shareableCode_01"


@pytest.mark.asyncio
async def test_join_ignores_owner_sub_disguised_as_invite(stub_join_service, fake_agent):
    body = _make_body()
    body.invite = "auth0|secret-owner"
    body.metadata = {"join_invite": "auth0|from-metadata", "owner": "auth0|nope"}
    with patch(
        "acn.routes.registry._resolve_registration_endpoint",
        new=AsyncMock(return_value=("https://probe.example.com/a2a", None, True, True)),
    ):
        await _join_agent_impl(
            body,
            BackgroundTasks(),
            ref=None,
            agent_service=stub_join_service,
            invite="auth0|from-query",
        )

    kwargs = stub_join_service.join_agent.await_args.kwargs
    meta = kwargs["metadata"] or {}
    assert "join_invite" not in meta
    assert "owner" not in meta
    assert "auth0|secret-owner" not in str(meta)


def test_public_agent_info_keeps_host_join_invite():
    """Gateway attribution reads GET /agents/{id} metadata; join_invite must survive strip."""
    from datetime import UTC, datetime

    agent = MagicMock()
    agent.agent_id = "agent-join-meta"
    agent.owner = None
    agent.name = "InviteProbe"
    agent.description = "Public read must keep Host invite code"
    agent.endpoint = "https://probe.example.com/a2a"
    agent.tags = []
    agent.subnet_ids = ["public"]
    agent.agent_card = None
    agent.metadata = {"join_invite": "ji_keepme_01", "visibility": "real"}
    agent.claim_status = None
    agent.referrer_id = None
    agent.verification_code = "SECRET-CLAIM-TOKEN"
    agent.registered_at = datetime.now(UTC)
    agent.last_heartbeat = None
    agent.wallet_address = None
    agent.wallet_addresses = None
    agent.accepts_payment = False
    agent.payment_methods = []
    agent.token_pricing = None
    agent.social_card_url = None
    agent.communication_policy = {"mode": "open"}

    info = _agent_entity_to_info(agent, is_online=True, strip_sensitive=True)
    assert info.metadata["join_invite"] == "ji_keepme_01"
    assert "verification_code" not in info.metadata


@pytest.mark.asyncio
async def test_join_emits_agent_registered_audit_for_real_visibility(stub_join_service):
    """Public joins should emit a system-level registration audit event."""
    with (
        patch(
            "acn.routes.registry._resolve_registration_endpoint",
            new=AsyncMock(return_value=("https://probe.example.com/a2a", None, True, True)),
        ),
        patch("acn.routes.registry.get_audit_singleton", return_value=object()),
        patch("acn.routes.registry.fire_and_forget_event") as fire,
    ):
        await _join_agent_impl(
            _make_body(),
            BackgroundTasks(),
            ref=None,
            agent_service=stub_join_service,
        )

    fire.assert_called_once()
    kwargs = fire.call_args.kwargs
    assert kwargs["event_type"] == AuditEventType.AGENT_REGISTERED
    assert kwargs["details"]["source"] == "join"
    assert kwargs["details"]["visibility"] == "real"
    assert kwargs["details"]["public_broadcast_eligible"] is True


@pytest.mark.asyncio
async def test_join_emits_internal_audit_for_non_real_visibility(
    stub_join_service,
    fake_agent,
):
    """Internal/test joins stay auditable but are marked non-public."""
    fake_agent.metadata = {"visibility": "test"}
    with (
        patch(
            "acn.routes.registry._resolve_registration_endpoint",
            new=AsyncMock(return_value=("https://probe.example.com/a2a", None, True, True)),
        ),
        patch("acn.routes.registry.get_audit_singleton", return_value=object()),
        patch("acn.routes.registry.fire_and_forget_event") as fire,
    ):
        await _join_agent_impl(
            _make_body(),
            BackgroundTasks(),
            ref=None,
            agent_service=stub_join_service,
            default_metadata={"visibility": "test"},
        )

    fire.assert_called_once()
    kwargs = fire.call_args.kwargs
    assert kwargs["event_type"] == AuditEventType.AGENT_REGISTERED
    assert kwargs["details"]["visibility"] == "test"
    assert kwargs["details"]["public_broadcast_eligible"] is False
