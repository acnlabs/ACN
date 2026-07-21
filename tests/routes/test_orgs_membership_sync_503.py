"""Regression: membership_sync_failed → HTTP 503 (ADR-0014 D4), not 409/500.

#176 originally set ``status=503`` on ``ACNHTTPError``, but that constructor
rejects 5xx and raised ``ValueError`` → unhandled 500. #179 briefly mapped
the reason to 409, which contradicts the ADR retry semantics. Pin 503 +
``Retry-After`` via ``HTTPException``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from acn.routes.orgs import OrgMemberAddRequest, add_member
from acn.services.org_service import OrgConflictError


@pytest.mark.asyncio
async def test_add_member_membership_sync_failed_returns_503():
    org_service = AsyncMock()
    org_service.add_member.side_effect = OrgConflictError(
        "membership_sync_failed",
        "subnet join succeeded but membership upsert failed",
    )
    request = MagicMock()
    body = OrgMemberAddRequest(agent_id="agt_worker", role="worker")
    payload = {"type": "agent", "sub": "agt_steward"}

    with pytest.raises(HTTPException) as ei:
        await add_member(
            request=request,
            body=body,
            org_id="org_test",
            payload=payload,
            org_service=org_service,
        )

    assert ei.value.status_code == 503
    assert ei.value.headers is not None
    assert ei.value.headers.get("Retry-After") == "5"
    assert "retry later" in str(ei.value.detail).lower()


@pytest.mark.asyncio
async def test_add_member_already_member_still_maps_to_acn_conflict():
    """Non-sync conflicts stay on the 409 RESOURCE_CONFLICT path."""
    from acn.core.errors import ACNHTTPError, ErrorCode

    org_service = AsyncMock()
    org_service.add_member.side_effect = OrgConflictError(
        "already_member",
        "agt_worker already in org",
    )
    request = MagicMock()
    body = OrgMemberAddRequest(agent_id="agt_worker")
    payload = {"type": "agent", "sub": "agt_steward"}

    with pytest.raises(ACNHTTPError) as ei:
        await add_member(
            request=request,
            body=body,
            org_id="org_test",
            payload=payload,
            org_service=org_service,
        )

    assert ei.value.status_code == 409
    assert ei.value.code == ErrorCode.RESOURCE_CONFLICT
    assert ei.value.details == {"reason": "already_member"}
