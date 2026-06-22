"""P3 transfer invite: PENDING_TRANSFER state machine + claim extension."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from acn.core.entities import Agent, ClaimStatus
from acn.core.exceptions import AgentNotFoundException
from acn.services.agent_service import AgentService, generate_verification_code


def _claimed_agent(**overrides) -> Agent:
    base = dict(
        agent_id="agt-gift",
        name="Gift Bot",
        owner="wechat|giver",
        claim_status=ClaimStatus.CLAIMED,
        verification_code=None,
    )
    base.update(overrides)
    return Agent(**base)


@pytest.fixture
def repo():
    return AsyncMock()


@pytest.fixture
def service(repo):
    return AgentService(repo)


@pytest.mark.asyncio
async def test_create_transfer_invite_sets_pending(service, repo):
    agent = _claimed_agent()
    repo.find_by_id.return_value = agent

    out = await service.create_transfer_invite("agt-gift", "wechat|giver", ttl_seconds=3600)

    assert out.claim_status == ClaimStatus.PENDING_TRANSFER
    assert out.verification_code
    assert out.owner == "wechat|giver"
    assert out.transfer_invite_expires_at() is not None
    repo.save.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_transfer_invite_rejects_non_owner(service, repo):
    repo.find_by_id.return_value = _claimed_agent()

    with pytest.raises(PermissionError):
        await service.create_transfer_invite("agt-gift", "wechat|other")


@pytest.mark.asyncio
async def test_create_transfer_invite_rejects_already_pending(service, repo):
    repo.find_by_id.return_value = _claimed_agent(
        claim_status=ClaimStatus.PENDING_TRANSFER,
        verification_code="tok",
        metadata={"transfer_invite_expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
    )

    with pytest.raises(ValueError, match="pending"):
        await service.create_transfer_invite("agt-gift", "wechat|giver")


@pytest.mark.asyncio
async def test_cancel_transfer_invite_restores_claimed(service, repo):
    exp = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    agent = _claimed_agent(
        claim_status=ClaimStatus.PENDING_TRANSFER,
        verification_code="tok",
        metadata={"transfer_invite_expires_at": exp},
    )
    repo.find_by_id.return_value = agent

    out = await service.cancel_transfer_invite("agt-gift", "wechat|giver")

    assert out.claim_status == ClaimStatus.CLAIMED
    assert out.verification_code is None
    assert "transfer_invite_expires_at" not in (out.metadata or {})
    repo.save.assert_awaited_once()


@pytest.mark.asyncio
async def test_claim_pending_transfer_succeeds(service, repo):
    code = generate_verification_code()
    exp = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    agent = _claimed_agent(
        claim_status=ClaimStatus.PENDING_TRANSFER,
        verification_code=code,
        metadata={"transfer_invite_expires_at": exp},
    )
    repo.find_by_id.return_value = agent

    out = await service.claim_agent("agt-gift", "wechat|recipient", verification_code=code)

    assert out.owner == "wechat|recipient"
    assert out.claim_status == ClaimStatus.CLAIMED
    assert out.verification_code is None
    repo.save.assert_awaited_once()


@pytest.mark.asyncio
async def test_claim_pending_transfer_expired_rejected(service, repo):
    code = generate_verification_code()
    exp = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    agent = _claimed_agent(
        claim_status=ClaimStatus.PENDING_TRANSFER,
        verification_code=code,
        metadata={"transfer_invite_expires_at": exp},
    )
    repo.find_by_id.return_value = agent

    with pytest.raises(ValueError, match="expired"):
        await service.claim_agent("agt-gift", "wechat|recipient", verification_code=code)


@pytest.mark.asyncio
async def test_claim_true_claimed_rejected(service, repo):
    repo.find_by_id.return_value = _claimed_agent()

    with pytest.raises(ValueError, match="already claimed"):
        await service.claim_agent("agt-gift", "wechat|recipient", verification_code="any")


@pytest.mark.asyncio
async def test_claim_wrong_code_rejected(service, repo):
    agent = _claimed_agent(
        claim_status=ClaimStatus.PENDING_TRANSFER,
        verification_code="correct",
        metadata={"transfer_invite_expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
    )
    repo.find_by_id.return_value = agent

    with pytest.raises(ValueError, match="Invalid"):
        await service.claim_agent("agt-gift", "wechat|recipient", verification_code="wrong")


@pytest.mark.asyncio
async def test_get_agent_not_found(service, repo):
    repo.find_by_id.return_value = None

    with pytest.raises(AgentNotFoundException):
        await service.create_transfer_invite("missing", "wechat|giver")
