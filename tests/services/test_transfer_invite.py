"""P3 transfer invite: PENDING_TRANSFER state machine + claim extension."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from acn.core.entities import Agent, ClaimStatus
from acn.core.exceptions import AgentNotFoundException
from acn.services.agent_service import (
    AgentService,
    generate_verification_code,
    hash_api_key,
)


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


@pytest.mark.asyncio
async def test_pending_transfer_blocks_direct_transfer(service, repo):
    repo.find_by_id.return_value = _claimed_agent(
        claim_status=ClaimStatus.PENDING_TRANSFER,
        verification_code="tok",
        metadata={"transfer_invite_expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
    )

    with pytest.raises(ValueError, match="pending transfer invite"):
        await service.transfer_agent("agt-gift", "wechat|giver", "wechat|other")


@pytest.mark.asyncio
async def test_pending_transfer_blocks_release(service, repo):
    repo.find_by_id.return_value = _claimed_agent(
        claim_status=ClaimStatus.PENDING_TRANSFER,
        verification_code="tok",
        metadata={"transfer_invite_expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
    )

    with pytest.raises(ValueError, match="pending transfer invite"):
        await service.release_agent("agt-gift", "wechat|giver")


@pytest.mark.asyncio
async def test_claim_emits_owner_changed(service, repo):
    """P3 claim must emit agent.owner_changed so Backend re-points the wallet."""
    exp = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    agent = _claimed_agent(
        claim_status=ClaimStatus.PENDING_TRANSFER,
        verification_code="tok",
        metadata={"transfer_invite_expires_at": exp},
    )
    repo.find_by_id.return_value = agent
    service.webhook_service = AsyncMock()

    await service.claim_agent("agt-gift", "wechat|recipient", verification_code="tok")

    service.webhook_service.send_event.assert_awaited_once()
    kwargs = service.webhook_service.send_event.await_args.kwargs
    assert kwargs["task_id"] == "agt-gift"
    assert kwargs["data"]["previous_owner"] == "wechat|giver"
    assert kwargs["data"]["new_owner"] == "wechat|recipient"
    assert kwargs["data"]["change_type"] == "claim"
    assert kwargs["outbox"] is True


@pytest.mark.asyncio
async def test_transfer_emits_owner_changed(service, repo):
    repo.find_by_id.return_value = _claimed_agent()
    service.webhook_service = AsyncMock()

    await service.transfer_agent("agt-gift", "wechat|giver", "wechat|new")

    kwargs = service.webhook_service.send_event.await_args.kwargs
    assert kwargs["data"]["new_owner"] == "wechat|new"
    assert kwargs["data"]["change_type"] == "transfer"


@pytest.mark.asyncio
async def test_release_emits_owner_changed_with_null_owner(service, repo):
    repo.find_by_id.return_value = _claimed_agent()
    service.webhook_service = AsyncMock()

    await service.release_agent("agt-gift", "wechat|giver")

    kwargs = service.webhook_service.send_event.await_args.kwargs
    assert kwargs["data"]["new_owner"] is None
    assert kwargs["data"]["change_type"] == "release"


@pytest.mark.asyncio
async def test_owner_changed_webhook_failure_does_not_break_claim(service, repo):
    """A webhook error must never roll back the ownership mutation."""
    exp = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    repo.find_by_id.return_value = _claimed_agent(
        claim_status=ClaimStatus.PENDING_TRANSFER,
        verification_code="tok",
        metadata={"transfer_invite_expires_at": exp},
    )
    service.webhook_service = AsyncMock()
    service.webhook_service.send_event.side_effect = RuntimeError("backend down")

    out = await service.claim_agent("agt-gift", "wechat|recipient", verification_code="tok")

    assert out.owner == "wechat|recipient"  # claim still succeeded
    repo.save.assert_awaited_once()


@pytest.mark.asyncio
async def test_transfer_invite_claim_rotates_api_key(service, repo):
    """P3 gift of a SELF-HOSTED agent rotates the key so the giver is locked out."""
    exp = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    old_hash = hash_api_key("acn_old_giver_key")
    agent = _claimed_agent(
        claim_status=ClaimStatus.PENDING_TRANSFER,
        verification_code="tok",
        api_key=old_hash,
        metadata={"transfer_invite_expires_at": exp, "self_hosted": True},
    )
    repo.find_by_id.return_value = agent

    out = await service.claim_agent("agt-gift", "wechat|recipient", verification_code="tok")

    assert out.api_key != old_hash  # rotated
    assert out.rotated_api_key  # plaintext surfaced for one-time delivery
    assert hash_api_key(out.rotated_api_key) == out.api_key  # stored hash matches


@pytest.mark.asyncio
async def test_managed_agent_with_key_is_not_rotated_on_claim(service, repo):
    """Platform/operator-managed agent (no self_hosted marker) keeps its key —
    the operator (e.g. AgentMother) re-keys on owner_changed, so rotating here
    would break the running instance."""
    exp = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    old_hash = hash_api_key("acn_managed_key")
    agent = _claimed_agent(
        claim_status=ClaimStatus.PENDING_TRANSFER,
        verification_code="tok",
        api_key=old_hash,
        metadata={"transfer_invite_expires_at": exp},  # no self_hosted
    )
    repo.find_by_id.return_value = agent

    out = await service.claim_agent("agt-gift", "wechat|recipient", verification_code="tok")

    assert out.api_key == old_hash  # NOT rotated
    assert out.rotated_api_key is None


@pytest.mark.asyncio
async def test_first_claim_does_not_rotate_api_key(service, repo):
    """Claiming a freshly-registered (unclaimed) agent must keep the deployer's key."""
    old_hash = hash_api_key("acn_deployer_key")
    agent = Agent(
        agent_id="agt-new",
        name="New Bot",
        owner=None,
        claim_status=ClaimStatus.UNCLAIMED,
        verification_code="regcode",
        api_key=old_hash,
    )
    repo.find_by_id.return_value = agent

    out = await service.claim_agent("agt-new", "wechat|deployer", verification_code="regcode")

    assert out.api_key == old_hash  # unchanged
    assert out.rotated_api_key is None


@pytest.mark.asyncio
async def test_transfer_rotates_self_hosted_but_does_not_surface_key(service, repo):
    old_hash = hash_api_key("acn_old_key")
    agent = _claimed_agent(api_key=old_hash, metadata={"self_hosted": True})
    repo.find_by_id.return_value = agent

    out = await service.transfer_agent("agt-gift", "wechat|giver", "wechat|new")

    assert out.api_key != old_hash  # rotated to lock out giver
    assert out.rotated_api_key  # present on entity (route decides not to return it)


@pytest.mark.asyncio
async def test_managed_agent_claim_skips_rotation(service, repo):
    """Platform-managed agents have no api_key — nothing to rotate."""
    exp = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    agent = _claimed_agent(
        claim_status=ClaimStatus.PENDING_TRANSFER,
        verification_code="tok",
        api_key=None,
        metadata={"transfer_invite_expires_at": exp},
    )
    repo.find_by_id.return_value = agent

    out = await service.claim_agent("agt-gift", "wechat|recipient", verification_code="tok")

    assert out.api_key is None
    assert out.rotated_api_key is None


@pytest.mark.asyncio
async def test_unclaimed_index_excludes_pending(repo):
    """PENDING_TRANSFER must never enter the public unclaimed pool."""
    from acn.core.entities import Agent

    pending = Agent(
        agent_id="agt-gift",
        name="Gift Bot",
        owner="wechat|giver",
        claim_status=ClaimStatus.PENDING_TRANSFER,
        verification_code="tok",
    )
    # is_claimed treats pending as claimed (owner-only ops still allowed),
    # but can_be_claimed allows the recipient's claim, and the agent is not
    # UNCLAIMED so unclaimed-pool filters skip it.
    assert pending.is_claimed() is True
    assert pending.can_be_claimed() is True
    assert pending.claim_status != ClaimStatus.UNCLAIMED
