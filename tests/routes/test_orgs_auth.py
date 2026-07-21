"""Route-level auth gate for Org Harness write paths + optional reader."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks, Request
from starlette.datastructures import Headers

from acn.core.errors import ACNHTTPError, ErrorCode
from acn.routes.orgs import require_org_auth, resolve_org_reader


@pytest.mark.asyncio
async def test_jwt_read_only_rejected_by_org_auth():
    checker = require_org_auth()
    request = MagicMock(spec=Request)
    request.state = MagicMock()
    request.headers = Headers({})
    credentials = MagicMock()
    credentials.credentials = "jwt-token"
    agent_service = AsyncMock()

    async def fake_verify(req, creds):
        return {"sub": "auth0|reader", "permissions": ["acn:read"], "type": "user"}

    with patch("acn.routes.orgs.verify_token", new=fake_verify), patch(
        "acn.routes.orgs.get_settings"
    ) as gs:
        gs.return_value = MagicMock(dev_mode=False, internal_api_token="tok")
        with pytest.raises(ACNHTTPError) as ei:
            await checker(
                request=request,
                background_tasks=BackgroundTasks(),
                credentials=credentials,
                x_internal_token=None,
                agent_service=agent_service,
            )
    assert ei.value.code == ErrorCode.MISSING_PERMISSION
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_jwt_write_accepted_by_org_auth():
    checker = require_org_auth()
    request = MagicMock(spec=Request)
    request.state = MagicMock()
    request.headers = Headers({})
    credentials = MagicMock()
    credentials.credentials = "jwt-token"
    agent_service = AsyncMock()

    async def fake_verify(req, creds):
        return {"sub": "auth0|writer", "permissions": ["acn:write"], "type": "user"}

    with patch("acn.routes.orgs.verify_token", new=fake_verify), patch(
        "acn.routes.orgs.get_settings"
    ) as gs:
        gs.return_value = MagicMock(dev_mode=False, internal_api_token="tok")
        payload = await checker(
            request=request,
            background_tasks=BackgroundTasks(),
            credentials=credentials,
            x_internal_token=None,
            agent_service=agent_service,
        )
    assert payload["sub"] == "auth0|writer"
    assert payload["type"] == "human"


# ---------------------------------------------------------------------------
# Optional reader dependency (private-Org read ACL) — never 401s;
# anonymous / invalid credentials resolve to None.
# ---------------------------------------------------------------------------


def _request() -> MagicMock:
    request = MagicMock(spec=Request)
    request.state = MagicMock()
    request.headers = Headers({})
    return request


@pytest.mark.asyncio
async def test_org_reader_anonymous_resolves_to_none():
    checker = resolve_org_reader()
    with patch("acn.routes.orgs.get_settings") as gs:
        gs.return_value = MagicMock(dev_mode=False, internal_api_token="tok")
        payload = await checker(
            request=_request(),
            background_tasks=BackgroundTasks(),
            credentials=None,
            x_internal_token=None,
            agent_service=AsyncMock(),
        )
    assert payload is None


@pytest.mark.asyncio
async def test_org_reader_invalid_jwt_resolves_to_none():
    checker = resolve_org_reader()
    credentials = MagicMock()
    credentials.credentials = "not-a-valid-jwt"

    async def boom(req, creds):
        raise RuntimeError("bad token")

    with patch("acn.routes.orgs.verify_token", new=boom), patch(
        "acn.routes.orgs.get_settings"
    ) as gs:
        gs.return_value = MagicMock(dev_mode=False, internal_api_token="tok")
        payload = await checker(
            request=_request(),
            background_tasks=BackgroundTasks(),
            credentials=credentials,
            x_internal_token=None,
            agent_service=AsyncMock(),
        )
    assert payload is None


@pytest.mark.asyncio
async def test_org_reader_invalid_agent_key_resolves_to_none():
    checker = resolve_org_reader()
    credentials = MagicMock()
    credentials.credentials = "acn_deadbeef"
    agent_service = AsyncMock()
    agent_service.get_agent_by_api_key.return_value = None

    with patch("acn.routes.orgs.get_settings") as gs:
        gs.return_value = MagicMock(dev_mode=False, internal_api_token="tok")
        payload = await checker(
            request=_request(),
            background_tasks=BackgroundTasks(),
            credentials=credentials,
            x_internal_token=None,
            agent_service=agent_service,
        )
    assert payload is None


@pytest.mark.asyncio
async def test_org_reader_agent_key_resolves_to_agent_payload():
    checker = resolve_org_reader()
    credentials = MagicMock()
    credentials.credentials = "acn_goodkey"
    agent = MagicMock()
    agent.agent_id = "agt_reader"
    agent_service = AsyncMock()
    agent_service.get_agent_by_api_key.return_value = agent

    with patch("acn.routes.orgs.get_settings") as gs:
        gs.return_value = MagicMock(dev_mode=False, internal_api_token="tok")
        payload = await checker(
            request=_request(),
            background_tasks=BackgroundTasks(),
            credentials=credentials,
            x_internal_token=None,
            agent_service=agent_service,
        )
    assert payload == {
        "sub": "agt_reader",
        "type": "agent",
        "permissions": ["acn:read", "acn:write"],
    }
