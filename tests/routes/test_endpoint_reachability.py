"""Tests for endpoint reachability probing at registration time.

ACN performs a two-layer check when an agent registers a direct ``a2a_endpoint``:

1. **DNS resolution** (hard fail): ``safe_resolve_target`` is called.
   - NXDOMAIN or a hostname that resolves to a private/blocked IP → 400.
   - This catches typos and unprovisioned DNS records before they enter
     the registry.

2. **HTTP HEAD probe** (hard fail): ``_probe_endpoint_http`` fires a HEAD
   request with a short timeout.
   - Any HTTP response (including 405 Method Not Allowed from a JSON-RPC-only
     server) counts as "reachable".
   - Connection failures and timeouts raise HTTPException(400) — registration
     is blocked. Agents must have a running, publicly reachable server before
     registering (or use communication_policy.mode='closed').

These tests are unit-level: they mock ``safe_resolve_target`` and
``_probe_endpoint_http`` so they never hit the network. The full
``_resolve_registration_endpoint`` and ``_join_agent_impl`` pipelines are
exercised so that integration between the two layers is also pinned.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks, HTTPException

from acn.routes.registry import (
    AgentJoinRequest,
    _check_endpoint_reachability,
    _join_agent_impl,
    _probe_endpoint_http,
    _resolve_registration_endpoint,
)
from acn.security import SSRFViolation

# ---------------------------------------------------------------------------
# _probe_endpoint_http — unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_returns_true_on_any_http_response():
    """Any HTTP response (including 405 from a POST-only server) counts as
    reachable. The probe must not inspect the status code."""
    import httpx

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 405  # Method Not Allowed — typical for JSON-RPC endpoint

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.head = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await _probe_endpoint_http("https://agent.example.com/a2a")

    assert result is True


@pytest.mark.asyncio
async def test_probe_returns_false_on_connect_error():
    """A ConnectError (connection refused, NXDOMAIN at TCP layer) → False."""
    import httpx

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.head = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client_cls.return_value = mock_client

        result = await _probe_endpoint_http("https://agent.example.com/a2a")

    assert result is False


@pytest.mark.asyncio
async def test_probe_returns_false_on_timeout():
    """A TimeoutException → False (server exists but didn't respond in time)."""
    import httpx

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.head = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_client_cls.return_value = mock_client

        result = await _probe_endpoint_http("https://agent.example.com/a2a")

    assert result is False


# ---------------------------------------------------------------------------
# _check_endpoint_reachability — unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dns_failure_raises_400():
    """A hostname that can't be resolved (NXDOMAIN) must raise HTTPException(400).

    This is the hard-fail layer: a completely unresolvable hostname should never
    enter the registry because ALL messages would silently fall to inbox fallback.
    """
    with patch(
        "acn.routes.registry.safe_resolve_target",
        new=AsyncMock(
            side_effect=SSRFViolation("DNS resolution failed for 'no-such-host.invalid': ...")
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await _check_endpoint_reachability("https://no-such-host.invalid/a2a")

    assert exc_info.value.status_code == 400
    assert "cannot be resolved" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_private_ip_resolution_raises_400():
    """A hostname that resolves to a private/blocked IP (DNS rebinding style)
    must also be rejected at registration time with a 400."""
    with patch(
        "acn.routes.registry.safe_resolve_target",
        new=AsyncMock(
            side_effect=SSRFViolation("Hostname 'evil.example.com' resolves to blocked address '10.0.0.1'")
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await _check_endpoint_reachability("https://evil.example.com/a2a")

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_dns_ok_http_probe_success_returns_true():
    """DNS resolves + HTTP probe succeeds → reachable=True."""
    with (
        patch(
            "acn.routes.registry.safe_resolve_target",
            new=AsyncMock(return_value=("agent.example.com", "1.2.3.4")),
        ),
        patch(
            "acn.routes.registry._probe_endpoint_http",
            new=AsyncMock(return_value=True),
        ),
    ):
        result = await _check_endpoint_reachability("https://agent.example.com/a2a")

    assert result is True


@pytest.mark.asyncio
async def test_dns_ok_http_probe_failure_raises_400():
    """DNS resolves but HTTP probe fails → HTTPException(400).
    Registration is now hard-blocked when the endpoint is unreachable."""
    with (
        patch(
            "acn.routes.registry.safe_resolve_target",
            new=AsyncMock(return_value=("agent.example.com", "1.2.3.4")),
        ),
        patch(
            "acn.routes.registry._probe_endpoint_http",
            new=AsyncMock(return_value=False),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await _check_endpoint_reachability("https://agent.example.com/a2a")

    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# _resolve_registration_endpoint — integration with reachability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_direct_endpoint_propagates_reachable_true():
    """When a direct endpoint is given and the probe succeeds, the third
    element of the returned tuple must be True."""
    with patch(
        "acn.routes.registry._check_endpoint_reachability",
        new=AsyncMock(return_value=True),
    ):
        endpoint, card, reachable = await _resolve_registration_endpoint(
            direct_endpoint="https://agent.example.com/a2a",
            agent_card_url=None,
            agent_card=None,
        )

    assert endpoint == "https://agent.example.com/a2a"
    assert card is None
    assert reachable is True


@pytest.mark.asyncio
async def test_resolve_direct_endpoint_raises_on_probe_failure():
    """When the HTTP probe fails, _resolve_registration_endpoint raises
    HTTPException(400) — registration is hard-blocked."""
    with patch(
        "acn.routes.registry._check_endpoint_reachability",
        new=AsyncMock(side_effect=HTTPException(status_code=400, detail="Endpoint did not respond")),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await _resolve_registration_endpoint(
                direct_endpoint="https://agent.example.com/a2a",
                agent_card_url=None,
                agent_card=None,
            )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_resolve_raises_on_dns_failure():
    """DNS failure propagates as HTTPException(400) through
    ``_resolve_registration_endpoint``."""
    with patch(
        "acn.routes.registry._check_endpoint_reachability",
        new=AsyncMock(side_effect=HTTPException(status_code=400, detail="Endpoint host cannot be resolved")),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await _resolve_registration_endpoint(
                direct_endpoint="https://no-such-host.invalid/a2a",
                agent_card_url=None,
                agent_card=None,
            )

    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# _join_agent_impl — endpoint_reachable surfaced in response
# ---------------------------------------------------------------------------


def _make_join_body() -> AgentJoinRequest:
    return AgentJoinRequest(
        name="ReachabilityTestAgent",
        description="Tests that endpoint_reachable is wired into the join response.",
        tags=["test"],
        a2a_endpoint="https://agent.example.com/a2a",
    )


def _make_fake_agent(verification_code: str = "test_code_abc") -> MagicMock:
    agent = MagicMock()
    agent.agent_id = "agent-reach-test-001"
    agent.name = "ReachabilityTestAgent"
    agent.status = MagicMock(value="active")
    agent.claim_status = MagicMock(value="unclaimed")
    agent.verification_code = verification_code
    # Explicit default so AgentJoinResponse can read a concrete mode
    # string from the stored agent. Individual tests may override.
    agent.communication_policy = {"mode": "open"}
    return agent


@pytest.mark.asyncio
async def test_join_response_endpoint_reachable_true_when_probe_succeeds():
    """When the endpoint probe succeeds, ``endpoint_reachable`` must be True
    in the ``AgentJoinResponse``."""
    fake_agent = _make_fake_agent()
    svc = AsyncMock()
    svc.join_agent = AsyncMock(return_value=(fake_agent, "acn_test_key"))

    with patch(
        "acn.routes.registry._resolve_registration_endpoint",
        new=AsyncMock(return_value=("https://agent.example.com/a2a", None, True)),
    ):
        resp = await _join_agent_impl(
            _make_join_body(),
            BackgroundTasks(),
            ref=None,
            agent_service=svc,
        )

    assert resp.endpoint_reachable is True


@pytest.mark.asyncio
async def test_join_blocked_when_probe_fails():
    """When the HTTP probe fails, _join_agent_impl must re-raise the
    HTTPException(400) — the agent must NOT be saved."""
    fake_agent = _make_fake_agent()
    svc = AsyncMock()
    svc.join_agent = AsyncMock(return_value=(fake_agent, "acn_test_key"))

    with patch(
        "acn.routes.registry._resolve_registration_endpoint",
        new=AsyncMock(
            side_effect=HTTPException(
                status_code=400,
                detail="Endpoint did not respond to a reachability probe.",
            )
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await _join_agent_impl(
                _make_join_body(),
                BackgroundTasks(),
                ref=None,
                agent_service=svc,
            )

    assert exc_info.value.status_code == 400
    svc.join_agent.assert_not_called()


@pytest.mark.asyncio
async def test_join_blocked_on_dns_failure():
    """If DNS resolution fails, ``_join_agent_impl`` must re-raise the
    HTTPException(400) — the agent must NOT be saved."""
    fake_agent = _make_fake_agent()
    svc = AsyncMock()
    svc.join_agent = AsyncMock(return_value=(fake_agent, "acn_test_key"))

    with patch(
        "acn.routes.registry._resolve_registration_endpoint",
        new=AsyncMock(
            side_effect=HTTPException(
                status_code=400,
                detail="Endpoint host cannot be resolved or is not allowed: ...",
            )
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await _join_agent_impl(
                _make_join_body(),
                BackgroundTasks(),
                ref=None,
                agent_service=svc,
            )

    assert exc_info.value.status_code == 400
    # join_agent must NOT have been called — agent was never persisted
    svc.join_agent.assert_not_called()


# ---------------------------------------------------------------------------
# #141 — ACN own gateway domain rejected at validation time
# ---------------------------------------------------------------------------


def test_join_request_rejects_acn_gateway_as_endpoint():
    """An endpoint that points at ACN's own gateway (GATEWAY_BASE_URL host)
    must be rejected at Pydantic validation time with a clear error message."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        AgentJoinRequest(
            name="bad-agent",
            description="Agent using ACN proxy as placeholder endpoint",
            a2a_endpoint="https://api.acnlabs.dev/api/v1/agents/some-uuid",
        )

    errors = exc_info.value.errors()
    assert any("ACN gateway" in str(e.get("msg", "")) for e in errors), (
        f"Expected 'ACN gateway' in error message, got: {errors}"
    )


def test_join_request_rejects_acn_gateway_subpath():
    """Any path under the ACN gateway host must be rejected — not just the
    exact gateway URL."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        AgentJoinRequest(
            name="bad-agent",
            description="Agent using ACN proxy as placeholder endpoint",
            endpoint="https://api.acnlabs.dev/anything",
        )

    errors = exc_info.value.errors()
    assert any("ACN gateway" in str(e.get("msg", "")) for e in errors)


def test_join_request_allows_non_acn_endpoint():
    """A well-formed endpoint on a non-ACN domain must pass validation."""
    req = AgentJoinRequest(
        name="good-agent",
        description="Agent with its own real endpoint",
        a2a_endpoint="https://agent.example.com/a2a",
    )
    assert req.a2a_endpoint == "https://agent.example.com/a2a"


# ---------------------------------------------------------------------------
# closed-mode: endpoint is optional
# ---------------------------------------------------------------------------


def test_join_request_closed_mode_allows_no_endpoint():
    """Agents in closed mode receive no inbound messages — endpoint is optional."""
    req = AgentJoinRequest(
        name="closed-agent",
        description="Agent that operates in closed mode without public endpoint",
        communication_policy={"mode": "closed"},
    )
    assert req.a2a_endpoint is None
    assert req.endpoint is None
    assert req.agent_card_url is None


def test_join_request_open_mode_still_requires_endpoint():
    """Open-mode agents still require an endpoint — the exemption is for
    non-pushing modes (manifest, closed)."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        AgentJoinRequest(
            name="open-agent",
            description="Open mode agent without any endpoint",
            communication_policy={"mode": "open"},
        )

    errors = exc_info.value.errors()
    assert any("endpoint" in str(e.get("msg", "")).lower() for e in errors)


# ---------------------------------------------------------------------------
# manifest-mode (default): endpoint is optional
# ---------------------------------------------------------------------------


def test_join_request_manifest_mode_allows_no_endpoint():
    """Manifest-mode agents pull from the manifest queue — they do not need
    a delivery endpoint. This is the path for pull-only AI assistants and
    local-dev agents without a public HTTP server."""
    req = AgentJoinRequest(
        name="pull-only-assistant",
        description="A conversational AI assistant with no HTTP server.",
        communication_policy={"mode": "manifest"},
    )
    assert req.a2a_endpoint is None
    assert req.endpoint is None
    assert req.agent_card_url is None


def test_join_request_default_mode_allows_no_endpoint():
    """When ``communication_policy`` is omitted, the default is ``manifest``
    — so registration must succeed without any URL field."""
    req = AgentJoinRequest(
        name="default-pull-agent",
        description="No policy, no endpoint — should default to manifest mode.",
    )
    assert req.a2a_endpoint is None
    assert (req.communication_policy or {}).get("mode") == "manifest"


def test_join_request_allowlist_mode_still_requires_endpoint():
    """Allowlist-mode agents may receive direct delivery from allowlisted
    senders — they still require a delivery endpoint."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        AgentJoinRequest(
            name="allowlist-agent",
            description="Allowlist mode agent without any endpoint",
            communication_policy={"mode": "allowlist"},
        )

    errors = exc_info.value.errors()
    msg = " ".join(str(e.get("msg", "")) for e in errors).lower()
    assert "endpoint" in msg
    # The error must steer the operator to the pull-based default, not
    # to 'closed' (which silently turns them into a black hole).
    assert "manifest" in msg


def test_join_request_open_mode_error_points_at_manifest_default():
    """The error message for ``open`` mode without endpoint must guide the
    operator toward the pull-based ``manifest`` default rather than the
    inbound-rejection ``closed`` escape hatch."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        AgentJoinRequest(
            name="open-agent",
            description="Open mode agent without any endpoint",
            communication_policy={"mode": "open"},
        )

    msg = " ".join(str(e.get("msg", "")) for e in exc_info.value.errors()).lower()
    assert "manifest" in msg


# ---------------------------------------------------------------------------
# _join_agent_impl — pull-only registration skips endpoint resolution
# ---------------------------------------------------------------------------


def _make_pull_only_join_body(mode: str = "manifest") -> AgentJoinRequest:
    return AgentJoinRequest(
        name="PullOnlyAgent",
        description="Agent registered without any HTTP delivery endpoint.",
        tags=["test"],
        communication_policy={"mode": mode},
    )


def _make_pull_only_agent(mode: str = "manifest") -> MagicMock:
    agent = MagicMock()
    agent.agent_id = "agent-pull-only-001"
    agent.name = "PullOnlyAgent"
    agent.status = MagicMock(value="active")
    agent.claim_status = MagicMock(value="unclaimed")
    agent.verification_code = "test_code_pull"
    agent.communication_policy = {"mode": mode}
    return agent


@pytest.mark.asyncio
async def test_join_impl_skips_endpoint_resolution_when_no_url():
    """A pull-only registration must not touch ``_resolve_registration_endpoint``
    at all — otherwise the DNS/probe hard-fail would block agents that have
    no endpoint by design."""
    svc = AsyncMock()
    svc.join_agent = AsyncMock(
        return_value=(_make_pull_only_agent("manifest"), "acn_test_key")
    )

    resolve_mock = AsyncMock()
    with patch(
        "acn.routes.registry._resolve_registration_endpoint",
        new=resolve_mock,
    ):
        resp = await _join_agent_impl(
            _make_pull_only_join_body("manifest"),
            BackgroundTasks(),
            ref=None,
            agent_service=svc,
        )

    resolve_mock.assert_not_called()
    # join_agent must receive endpoint=None — not a coerced empty string,
    # which would still pass downstream "is this set?" checks.
    kwargs = svc.join_agent.await_args.kwargs
    assert kwargs["endpoint"] is None
    assert kwargs["a2a_endpoint"] is None
    assert resp.communication_mode == "manifest"


@pytest.mark.asyncio
async def test_pull_only_registration_reports_endpoint_not_reachable():
    """A pull-only registration has no endpoint to probe — the response
    must report ``endpoint_reachable=False`` rather than the misleading
    True default. Senders rely on this flag to decide whether direct
    delivery is even possible."""
    svc = AsyncMock()
    svc.join_agent = AsyncMock(
        return_value=(_make_pull_only_agent("manifest"), "acn_test_key")
    )

    with patch(
        "acn.routes.registry._resolve_registration_endpoint",
        new=AsyncMock(),
    ):
        resp = await _join_agent_impl(
            _make_pull_only_join_body("manifest"),
            BackgroundTasks(),
            ref=None,
            agent_service=svc,
        )

    assert resp.endpoint_reachable is False


@pytest.mark.asyncio
async def test_join_response_includes_manifest_next_step_hint():
    """Pull-only manifest agents must receive a next_step_hint that names
    the manifest poll URL — so the operator immediately knows how to
    receive messages."""
    svc = AsyncMock()
    svc.join_agent = AsyncMock(
        return_value=(_make_pull_only_agent("manifest"), "acn_test_key")
    )

    with patch(
        "acn.routes.registry._resolve_registration_endpoint",
        new=AsyncMock(),
    ):
        resp = await _join_agent_impl(
            _make_pull_only_join_body("manifest"),
            BackgroundTasks(),
            ref=None,
            agent_service=svc,
        )

    assert resp.next_step_hint is not None
    assert "manifest" in resp.next_step_hint.lower()
    assert "/communication/manifest/" in resp.next_step_hint


@pytest.mark.asyncio
async def test_join_response_includes_closed_next_step_hint():
    """Closed-mode pull-only registrations must receive a hint explaining
    that the agent will reject all inbound messages until the operator
    switches the mode."""
    svc = AsyncMock()
    svc.join_agent = AsyncMock(
        return_value=(_make_pull_only_agent("closed"), "acn_test_key")
    )

    with patch(
        "acn.routes.registry._resolve_registration_endpoint",
        new=AsyncMock(),
    ):
        resp = await _join_agent_impl(
            _make_pull_only_join_body("closed"),
            BackgroundTasks(),
            ref=None,
            agent_service=svc,
        )

    assert resp.next_step_hint is not None
    assert "closed" in resp.next_step_hint.lower()
    assert "/policy" in resp.next_step_hint


@pytest.mark.asyncio
async def test_join_response_no_hint_on_open_happy_path():
    """An ``open``-mode registration with a reachable endpoint is the happy
    path — no follow-up action is required, so ``next_step_hint`` must
    be ``None``."""
    fake_agent = _make_fake_agent()
    fake_agent.communication_policy = {"mode": "open"}
    svc = AsyncMock()
    svc.join_agent = AsyncMock(return_value=(fake_agent, "acn_test_key"))

    body = AgentJoinRequest(
        name="ReachabilityTestAgent",
        description="Tests that no hint is emitted on the happy path.",
        tags=["test"],
        a2a_endpoint="https://agent.example.com/a2a",
        communication_policy={"mode": "open"},
    )

    with patch(
        "acn.routes.registry._resolve_registration_endpoint",
        new=AsyncMock(return_value=("https://agent.example.com/a2a", None, True)),
    ):
        resp = await _join_agent_impl(
            body,
            BackgroundTasks(),
            ref=None,
            agent_service=svc,
        )

    assert resp.communication_mode == "open"
    assert resp.next_step_hint is None


@pytest.mark.asyncio
async def test_join_response_hint_when_endpoint_unreachable():
    """When an endpoint is registered but the probe returns False (soft
    case — DNS resolved but server isn't up yet), the hint must tell
    the operator that messages will queue until the server answers."""
    fake_agent = _make_fake_agent()
    fake_agent.communication_policy = {"mode": "open"}
    svc = AsyncMock()
    svc.join_agent = AsyncMock(return_value=(fake_agent, "acn_test_key"))

    body = AgentJoinRequest(
        name="ReachabilityTestAgent",
        description="Tests the unreachable-endpoint hint.",
        tags=["test"],
        a2a_endpoint="https://agent.example.com/a2a",
        communication_policy={"mode": "open"},
    )

    with patch(
        "acn.routes.registry._resolve_registration_endpoint",
        new=AsyncMock(return_value=("https://agent.example.com/a2a", None, False)),
    ):
        resp = await _join_agent_impl(
            body,
            BackgroundTasks(),
            ref=None,
            agent_service=svc,
        )

    assert resp.endpoint_reachable is False
    assert resp.next_step_hint is not None
    assert "reachability" in resp.next_step_hint.lower()
