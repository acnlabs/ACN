"""Python SDK regression tests for ``CommunicationProfile`` and
``update_policy`` — the manifest-mode reachability fields shipped in
PR #87.

These tests pin:

* ``CommunicationProfile.unread_manifest_count`` round-trips from the
  server payload and falls back to ``0`` for older servers (or test
  harnesses) that don't include the field. The default keeps callers
  byte-identical when running against a pre-PR-87 server.
* ``update_policy`` returns the raw server dict unmodified, including
  the conditional ``warning`` field that the server emits only when
  the post-update mode requires polling (``manifest`` /
  ``allowlist``). The SDK deliberately does not wrap this in a
  Pydantic model — see ``test_rotate_api_key.py`` for the same
  forward-compat trade-off across the admission / rotate-key surface.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from acn_client.client import ACNClient
from acn_client.models import CommunicationProfile


def _make_client_with_stub(request_mock: AsyncMock) -> ACNClient:
    """ACNClient with ``_request`` replaced by an awaitable stub.

    Mirrors the helper in ``test_rotate_api_key.py`` /
    ``test_subnet_admission.py`` so all SDK regression suites share
    the same shape.
    """
    client = ACNClient(base_url="http://acn.test")
    client._request = request_mock  # type: ignore[method-assign]
    return client


# ---------------------------------------------------------------------------
# CommunicationProfile.unread_manifest_count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_communication_profile_surfaces_unread_count():
    """Server PR #87 always populates ``unread_manifest_count``. The
    SDK must surface it on the typed model so senders can detect
    queue buildup before deciding to attach an attention_fee.
    """
    payload: dict[str, Any] = {
        "agent_id": "agent-1",
        "mode": "manifest",
        "attention_fee_required": False,
        "unread_manifest_count": 7,
    }
    request_mock = AsyncMock(return_value=payload)
    client = _make_client_with_stub(request_mock)

    profile = await client.get_communication_profile("agent-1")

    assert isinstance(profile, CommunicationProfile)
    assert profile.agent_id == "agent-1"
    assert profile.mode == "manifest"
    assert profile.attention_fee_required is False
    assert profile.unread_manifest_count == 7
    request_mock.assert_awaited_once_with(
        "GET", "/api/v1/agents/agent-1/communication_profile"
    )


@pytest.mark.asyncio
async def test_get_communication_profile_back_compat_pre_pr87_server():
    """A server without PR #87 (or a test harness sending the legacy
    three-field payload) must not cause a Pydantic validation error.
    The default of ``0`` keeps the SDK usable against older deploys
    and against unit tests that hand-craft minimal payloads.
    """
    payload: dict[str, Any] = {
        "agent_id": "legacy-agent",
        "mode": "open",
        "attention_fee_required": False,
        # NOTE: ``unread_manifest_count`` deliberately absent.
    }
    request_mock = AsyncMock(return_value=payload)
    client = _make_client_with_stub(request_mock)

    profile = await client.get_communication_profile("legacy-agent")

    assert profile.unread_manifest_count == 0


# ---------------------------------------------------------------------------
# update_policy — conditional ``warning`` field
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_policy_passes_through_warning_on_manifest_mode():
    """When the post-update mode requires polling
    (``'manifest'`` / ``'allowlist'``), the server emits a
    ``warning`` string. The SDK returns the raw server dict so
    operators can surface this in CLIs / dashboards.
    """
    server_payload: dict[str, Any] = {
        "agent_id": "agent-1",
        "communication_policy": {"mode": "manifest"},
        "warning": (
            "Messages from non-trusted senders will be diverted to the "
            "manifest queue. Your agent must periodically poll "
            "GET /communication/manifest/{id} to receive them. Without "
            "active polling, these messages are unreachable and will "
            "expire after the configured TTL (default 7 days)."
        ),
    }
    request_mock = AsyncMock(return_value=server_payload)
    client = _make_client_with_stub(request_mock)

    result = await client.update_policy("agent-1", "manifest")

    assert result is server_payload
    assert "warning" in result
    assert "manifest queue" in result["warning"]
    request_mock.assert_awaited_once_with(
        "PATCH",
        "/api/v1/agents/agent-1/policy",
        json={"communication_policy": {"mode": "manifest"}},
    )


@pytest.mark.asyncio
async def test_update_policy_omits_warning_on_open_mode():
    """``mode='open'`` does not gate inbound messages, so the server
    does NOT emit ``warning``. The SDK must not synthesise one.
    """
    server_payload: dict[str, Any] = {
        "agent_id": "agent-2",
        "communication_policy": {"mode": "open"},
    }
    request_mock = AsyncMock(return_value=server_payload)
    client = _make_client_with_stub(request_mock)

    result = await client.update_policy("agent-2", "open")

    assert result is server_payload
    assert "warning" not in result
