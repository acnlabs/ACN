"""Org publish-task — attribution vs Org-paid (org-wallet-v0 B/C)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request

from acn.core.entities.org import Org, OrgOwner, OrgPrincipal
from acn.core.entities.task import Task, TaskStatus
from acn.core.errors import ACNHTTPError
from acn.routes.orgs import OrgPublishTaskRequest, publish_org_task
from acn.services.org_service import OrgPermissionError, OrgService


def _org(org_id: str = "org_pay_1") -> Org:
    return Org(
        org_id=org_id,
        display_name="Pay Co",
        created_by=OrgPrincipal(kind="agent", subject="agt_creator"),
        subnet_id="pay-co",
        owner=OrgOwner(kind="none"),
        steward_agent_id="agt_creator",
        status="active",
    )


def _task(**kwargs) -> Task:
    base = {
        "task_id": "task_pub_1",
        "creator_type": "agent",
        "creator_id": "agt_creator",
        "creator_name": "agt_creator",
        "title": "Need a reviewer please",
        "description": "Review the adapter PR and leave notes.",
        "status": TaskStatus.OPEN,
        "reward": "0",
        "reward_currency": "ap_points",
        "use_escrow": False,
        "metadata": {"org_id": "org_pay_1", "org_publish": True},
    }
    base.update(kwargs)
    return Task(**base)


def _services():
    org_svc = MagicMock(spec=OrgService)
    org_svc.get_org = AsyncMock(return_value=_org())
    org_svc.assert_treasury_principal = MagicMock()
    org_svc._agent_in_subnet = AsyncMock(return_value=True)
    task_svc = MagicMock()
    task_svc.create_task = AsyncMock(return_value=_task())
    return org_svc, task_svc


@pytest.mark.asyncio
async def test_publish_attribution_default():
    org_svc, task_svc = _services()
    body = OrgPublishTaskRequest(
        title="Need a reviewer please",
        description="Review the adapter PR and leave notes.",
        required_tags=["review"],
        reward="0",
    )
    await publish_org_task(
        request=MagicMock(spec=Request),
        body=body,
        org_id="org_pay_1",
        payload={"sub": "agt_creator", "type": "agent"},
        org_service=org_svc,
        task_service=task_svc,
    )
    org_svc.assert_treasury_principal.assert_not_called()
    kwargs = task_svc.create_task.await_args.kwargs
    assert kwargs["creator_type"] == "agent"
    assert kwargs["creator_id"] == "agt_creator"
    assert kwargs["reward_currency"] == "ap_points"
    assert kwargs["use_escrow"] is False
    assert kwargs["metadata"]["org_publish"] is True


@pytest.mark.asyncio
async def test_publish_pay_from_org_forces_credits_escrow():
    org_svc, task_svc = _services()
    task_svc.create_task = AsyncMock(
        return_value=_task(
            creator_type="org",
            creator_id="org_pay_1",
            reward="100",
            reward_currency="credits",
            use_escrow=True,
        )
    )
    body = OrgPublishTaskRequest(
        title="Need a reviewer please",
        description="Review the adapter PR and leave notes.",
        required_tags=["review"],
        reward="100",
        pay_from_org=True,
    )
    await publish_org_task(
        request=MagicMock(spec=Request),
        body=body,
        org_id="org_pay_1",
        payload={"sub": "agt_creator", "type": "agent"},
        org_service=org_svc,
        task_service=task_svc,
    )
    org_svc.assert_treasury_principal.assert_called_once()
    kwargs = task_svc.create_task.await_args.kwargs
    assert kwargs["creator_type"] == "org"
    assert kwargs["creator_id"] == "org_pay_1"
    assert kwargs["reward_currency"] == "credits"
    assert kwargs["use_escrow"] is True
    assert kwargs["metadata"]["org_pay"] is True


@pytest.mark.asyncio
async def test_publish_pay_from_org_denied():
    org_svc, task_svc = _services()
    org_svc.assert_treasury_principal.side_effect = OrgPermissionError(
        "ownership_mismatch", "Caller is not Org owner"
    )
    body = OrgPublishTaskRequest(
        title="Need a reviewer please",
        description="Review the adapter PR and leave notes.",
        required_tags=["review"],
        reward="50",
        pay_from_org=True,
    )
    with pytest.raises(ACNHTTPError) as ei:
        await publish_org_task(
            request=MagicMock(spec=Request),
            body=body,
            org_id="org_pay_1",
            payload={"sub": "agt_other", "type": "agent"},
            org_service=org_svc,
            task_service=task_svc,
        )
    assert ei.value.status_code == 403
    task_svc.create_task.assert_not_called()
