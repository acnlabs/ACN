"""Tests for endpoint reachability probing at registration time.

ACN performs a two-layer check when an agent registers a direct ``a2a_endpoint``:

1. **DNS resolution** (hard fail): ``safe_resolve_target`` is called.
   - NXDOMAIN or a hostname that resolves to a private/blocked IP → 400.
   - This catches typos and unprovisioned DNS records before they enter
     the registry.

2. **HTTP HEAD probe** (soft warning): ``_probe_endpoint_http`` fires a HEAD
   request with a short timeout.
   - Any HTTP response (including 405 Method Not Allowed from a JSON-RPC-only
     server) counts as "reachable".
   - Connection failures and timeouts set ``endpoint_reachable=False`` in the
     join response without blocking registration — the server may not be
     deployed yet at join time.

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
async def test_dns_ok_http_probe_failure_returns_false():
    """DNS resolves but HTTP probe fails → reachable=False (soft warning,
    registration is NOT blocked)."""
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
        result = await _check_endpoint_reachability("https://agent.example.com/a2a")

    assert result is False


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
async def test_resolve_direct_endpoint_propagates_reachable_false():
    """When the HTTP probe fails the tuple carries reachable=False but does
    NOT raise — registration continues with a soft warning."""
    with patch(
        "acn.routes.registry._check_endpoint_reachability",
        new=AsyncMock(return_value=False),
    ):
        endpoint, card, reachable = await _resolve_registration_endpoint(
            direct_endpoint="https://agent.example.com/a2a",
            agent_card_url=None,
            agent_card=None,
        )

    assert endpoint == "https://agent.example.com/a2a"
    assert reachable is False


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
async def test_join_response_endpoint_reachable_false_when_probe_fails():
    """When the HTTP probe fails (server not yet up), ``endpoint_reachable``
    must be False in the response — but registration MUST succeed (no raise)."""
    fake_agent = _make_fake_agent()
    svc = AsyncMock()
    svc.join_agent = AsyncMock(return_value=(fake_agent, "acn_test_key"))

    with patch(
        "acn.routes.registry._resolve_registration_endpoint",
        new=AsyncMock(return_value=("https://agent.example.com/a2a", None, False)),
    ):
        resp = await _join_agent_impl(
            _make_join_body(),
            BackgroundTasks(),
            ref=None,
            agent_service=svc,
        )

    # Registration succeeded (no exception raised)
    assert resp.agent_id == fake_agent.agent_id
    # Soft warning surfaced in response
    assert resp.endpoint_reachable is False


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
