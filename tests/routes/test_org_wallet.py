"""GET /orgs/{id}/wallet — treasury-gated Backend proxy (org-wallet-v0 S6)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request

from acn.core.entities.org import Org, OrgOwner, OrgPrincipal
from acn.core.errors import ACNHTTPError
from acn.routes.orgs import get_org_wallet
from acn.services.org_service import OrgPermissionError, OrgService
from acn.services.wallet_client import OrgWalletSummary


def _org(org_id: str = "org_w_1") -> Org:
    return Org(
        org_id=org_id,
        display_name="Wallet Co",
        created_by=OrgPrincipal(kind="agent", subject="agt_creator"),
        subnet_id="wallet-co",
        owner=OrgOwner(kind="none"),
        steward_agent_id="agt_creator",
        status="active",
    )


@pytest.mark.asyncio
async def test_get_org_wallet_ok():
    org_svc = MagicMock(spec=OrgService)
    org_svc.get_org = AsyncMock(return_value=_org())
    org_svc.assert_treasury_principal = MagicMock()
    summary = OrgWalletSummary(
        org_id="org_w_1",
        exists=True,
        wallet_id="w1",
        balance=42,
        owner_id="agt_creator",
        spend_autonomy="disabled",
        status="active",
    )
    with (
        patch("acn.routes.orgs.get_settings") as gs,
        patch("acn.services.wallet_client.WalletClient") as WC,
    ):
        gs.return_value = MagicMock(
            backend_url="http://backend:8000",
            internal_api_token="t" * 32,
        )
        WC.return_value.get_org_wallet = AsyncMock(return_value=summary)
        out = await get_org_wallet(
            request=MagicMock(spec=Request),
            org_id="org_w_1",
            payload={"sub": "agt_creator", "type": "agent"},
            org_service=org_svc,
        )
    org_svc.assert_treasury_principal.assert_called_once()
    assert out["exists"] is True
    assert out["balance"] == 42
    assert out["org_id"] == "org_w_1"


@pytest.mark.asyncio
async def test_get_org_wallet_forbidden():
    org_svc = MagicMock(spec=OrgService)
    org_svc.get_org = AsyncMock(return_value=_org())
    org_svc.assert_treasury_principal = MagicMock(
        side_effect=OrgPermissionError("ownership_mismatch", "nope")
    )
    with pytest.raises(ACNHTTPError) as ei:
        await get_org_wallet(
            request=MagicMock(spec=Request),
            org_id="org_w_1",
            payload={"sub": "agt_other", "type": "agent"},
            org_service=org_svc,
        )
    assert ei.value.status_code == 403
