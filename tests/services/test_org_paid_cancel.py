"""Org-paid task cancel — treasury principal may cancel for escrow refund."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.core.entities.org import Org, OrgOwner, OrgPrincipal
from acn.core.entities.task import Task, TaskStatus
from acn.services.org_service import OrgPermissionError, OrgService
from acn.services.task_service import TaskService


def _org() -> Org:
    return Org(
        org_id="org_pay_1",
        display_name="Pay Co",
        created_by=OrgPrincipal(kind="agent", subject="agt_creator"),
        subnet_id="pay-co",
        owner=OrgOwner(kind="none"),
        steward_agent_id="agt_creator",
        status="active",
    )


@pytest.mark.asyncio
async def test_org_paid_cancel_allows_treasury():
    task = Task(
        task_id="t1",
        creator_type="org",
        creator_id="org_pay_1",
        creator_name="Pay Co",
        title="Need a reviewer please",
        description="Review the adapter PR and leave notes.",
        status=TaskStatus.OPEN,
        reward="100",
        reward_currency="credits",
        use_escrow=True,
        metadata={"org_id": "org_pay_1", "org_pay": True},
    )
    repo = MagicMock()
    repo.find_by_id = AsyncMock(return_value=task)
    repo.compare_and_save = AsyncMock(return_value=True)

    org_svc = MagicMock(spec=OrgService)
    org_svc.get_org = AsyncMock(return_value=_org())
    org_svc.assert_treasury_principal = MagicMock()

    svc = TaskService(repository=repo, org_service=org_svc)
    result = await svc.cancel_task(
        "t1", "agt_creator", canceller_type="agent"
    )
    assert result.status == TaskStatus.CANCELLED
    org_svc.assert_treasury_principal.assert_called_once()


@pytest.mark.asyncio
async def test_org_paid_cancel_denies_non_treasury():
    task = Task(
        task_id="t2",
        creator_type="org",
        creator_id="org_pay_1",
        creator_name="Pay Co",
        title="Need a reviewer please",
        description="Review the adapter PR and leave notes.",
        status=TaskStatus.OPEN,
        reward="100",
        reward_currency="credits",
        use_escrow=True,
    )
    repo = MagicMock()
    repo.find_by_id = AsyncMock(return_value=task)

    org_svc = MagicMock(spec=OrgService)
    org_svc.get_org = AsyncMock(return_value=_org())
    org_svc.assert_treasury_principal.side_effect = OrgPermissionError(
        "ownership_mismatch", "Caller is not Org owner"
    )

    svc = TaskService(repository=repo, org_service=org_svc)
    with pytest.raises(PermissionError):
        await svc.cancel_task("t2", "agt_other", canceller_type="agent")
    repo.compare_and_save.assert_not_called()
