"""Python SDK regression tests for ``rotate_api_key`` (H1 — pre-launch
security audit).

The SDK method shipped in 0.10.0 alongside the server-side endpoint, but
without unit-test coverage. This file pins:

* The wire shape: ``POST /api/v1/agents/{id}/rotate-key`` with no body.
* The return shape: a plain ``dict`` mirroring the server's
  ``AgentRotateKeyResponse`` (``success``, ``agent_id``, ``api_key``,
  ``message``) — no Pydantic wrapping, by design, to keep the field
  surface forward-compatible if the server later adds fields.
* Idiomatic use: caller must persist ``payload["api_key"]`` because the
  server immediately invalidates the previous key (auth cache eviction
  is a server-side side effect — the SDK has no rotate-side hook).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from acn_client.client import ACNClient


def _make_client_with_stub(request_mock: AsyncMock) -> ACNClient:
    """ACNClient with ``_request`` replaced by an awaitable stub.

    Mirrors the helper in ``test_subnet_nesting.py`` so this file
    follows the same pattern other SDK regression suites already use.
    """
    client = ACNClient(base_url="http://acn.test")
    client._request = request_mock  # type: ignore[method-assign]
    return client


@pytest.mark.asyncio
async def test_rotate_api_key_posts_to_canonical_path():
    """The wire path must match the server's ``/rotate-key`` route — no
    body, no params, agent_id is path-only.
    """
    payload: dict[str, Any] = {
        "success": True,
        "agent_id": "agent-1",
        "api_key": "acn_NEW_PLAINTEXT_xyz",
        "message": "API key rotated. Previous key is now invalid.",
    }
    request_mock = AsyncMock(return_value=payload)
    client = _make_client_with_stub(request_mock)

    result = await client.rotate_api_key("agent-1")

    request_mock.assert_awaited_once_with(
        "POST", "/api/v1/agents/agent-1/rotate-key"
    )
    assert result is payload


@pytest.mark.asyncio
async def test_rotate_api_key_returns_raw_server_payload():
    """We deliberately do not wrap the response in a Pydantic model.
    The whole point of the H1 contract is the new ``api_key`` plaintext;
    a too-tight schema would force a SDK release every time the server
    adds a field. Keep it as ``dict[str, Any]`` and pin the four fields
    callers actually depend on.
    """
    payload = {
        "success": True,
        "agent_id": "agent-42",
        "api_key": "acn_AnotherFreshKey_abc",
        "message": "rotated",
        # Forward-compat: a future server might add fields like
        # ``rotated_at`` or ``previous_key_invalidated_at``. The SDK
        # must not strip them. ``dict[str, Any]`` is exactly that
        # affordance.
        "rotated_at": "2026-05-18T15:00:00Z",
    }
    request_mock = AsyncMock(return_value=payload)
    client = _make_client_with_stub(request_mock)

    result = await client.rotate_api_key("agent-42")

    assert result["api_key"] == "acn_AnotherFreshKey_abc"
    assert result["rotated_at"] == "2026-05-18T15:00:00Z"


@pytest.mark.asyncio
async def test_rotate_api_key_passes_agent_id_into_path():
    """Regression: ensure agent_id is path-encoded by httpx via the
    underlying ``_request`` call, not silently moved into a body field
    or query param. A bug here would invalidate the wrong agent's key.
    """
    request_mock = AsyncMock(
        return_value={"success": True, "agent_id": "weird:id", "api_key": "x"}
    )
    client = _make_client_with_stub(request_mock)

    await client.rotate_api_key("weird:id")

    # Match exactly — neither ``json=`` nor ``params=`` should be set
    # by the SDK on the rotate path.
    request_mock.assert_awaited_once_with(
        "POST", "/api/v1/agents/weird:id/rotate-key"
    )
