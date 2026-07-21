"""Route-level auth gate for Org Harness write paths."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks, Request
from starlette.datastructures import Headers

from acn.core.errors import ACNHTTPError, ErrorCode
from acn.routes.orgs import require_org_auth


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
