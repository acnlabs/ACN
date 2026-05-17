"""Unit Tests for AgentService

Tests business logic with mocked repositories.
"""

import pytest

from acn.core.entities import AgentStatus
from acn.core.exceptions import AgentNotFoundException
from acn.services import AgentService


class TestAgentService:
    """Test AgentService business logic"""

    @pytest.mark.asyncio
    async def test_register_new_agent(self, mock_agent_repository):
        """Test registering a new agent"""
        # Setup mock - no existing agent
        mock_agent_repository.find_by_owner_and_endpoint.return_value = None

        service = AgentService(mock_agent_repository)

        agent = await service.register_agent(
            owner="user-123",
            name="Test Agent",
            endpoint="https://agent.example.com",
            tags=["task-planning"],
        )

        # Verify agent created
        assert agent.owner == "user-123"
        assert agent.name == "Test Agent"
        assert agent.endpoint == "https://agent.example.com"
        assert agent.tags == ["task-planning"]
        assert agent.status == AgentStatus.ONLINE

        # Verify repository called
        mock_agent_repository.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_register_existing_agent_updates(self, mock_agent_repository, sample_agent):
        """Test re-registering existing agent updates it"""
        # Setup mock - existing agent found
        mock_agent_repository.find_by_owner_and_endpoint.return_value = sample_agent

        service = AgentService(mock_agent_repository)

        agent = await service.register_agent(
            owner=sample_agent.owner,
            name="Updated Name",
            endpoint=sample_agent.endpoint,
            tags=["new-skill"],
        )

        # Verify agent updated
        assert agent.agent_id == sample_agent.agent_id  # Same ID
        assert agent.name == "Updated Name"  # Updated
        assert agent.tags == ["new-skill"]  # Updated
        assert agent.status == AgentStatus.ONLINE

        # Verify repository save called
        mock_agent_repository.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_agent_success(self, mock_agent_repository, sample_agent):
        """Test getting an existing agent"""
        # Setup mock
        mock_agent_repository.find_by_id.return_value = sample_agent

        service = AgentService(mock_agent_repository)

        agent = await service.get_agent(sample_agent.agent_id)

        assert agent.agent_id == sample_agent.agent_id
        assert agent.name == sample_agent.name

        mock_agent_repository.find_by_id.assert_called_once_with(sample_agent.agent_id)

    @pytest.mark.asyncio
    async def test_get_agent_not_found(self, mock_agent_repository):
        """Test getting non-existent agent raises exception"""
        # Setup mock - agent not found
        mock_agent_repository.find_by_id.return_value = None

        service = AgentService(mock_agent_repository)

        with pytest.raises(AgentNotFoundException):
            await service.get_agent("non-existent-id")

    @pytest.mark.asyncio
    async def test_search_agents_by_skills(self, mock_agent_repository, sample_agent):
        """Test searching agents by skills"""
        # Setup mock
        mock_agent_repository.find_by_tags.return_value = [sample_agent]
        mock_agent_repository.filter_alive.return_value = {sample_agent.agent_id}

        service = AgentService(mock_agent_repository)

        agents = await service.search_agents(
            tags=["task-planning"],
            status="online",
        )

        assert len(agents) == 1
        assert agents[0].agent_id == sample_agent.agent_id

        mock_agent_repository.find_by_tags.assert_called_once_with(["task-planning"], "online")

    @pytest.mark.asyncio
    async def test_search_agents_by_subnet(self, mock_agent_repository, sample_agent):
        """Test searching agents by subnet"""
        # Setup mock
        mock_agent_repository.find_by_subnet.return_value = [sample_agent]
        mock_agent_repository.filter_alive.return_value = {sample_agent.agent_id}

        service = AgentService(mock_agent_repository)

        agents = await service.search_agents(subnet_id="public")

        assert len(agents) == 1
        assert agents[0].agent_id == sample_agent.agent_id

        mock_agent_repository.find_by_subnet.assert_called_once_with("public")

    @pytest.mark.asyncio
    async def test_update_heartbeat(self, mock_agent_repository, sample_agent):
        """Test updating agent heartbeat"""
        # Setup mock
        mock_agent_repository.find_by_id.return_value = sample_agent

        service = AgentService(mock_agent_repository)

        agent = await service.update_heartbeat(sample_agent.agent_id)

        assert agent.last_heartbeat is not None
        assert agent.status == AgentStatus.ONLINE

        mock_agent_repository.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_touch_alive_renews_redis_ttl_only(self, mock_agent_repository):
        """``touch_alive`` is the implicit-heartbeat fast path.

        Contract: it refreshes only the Redis ``alive`` key
        (``repository.set_alive`` with ``ALIVE_RENEW_TTL``) and does
        NOT load the Agent row or call ``repository.save`` — the
        whole point of this method is that it stays cheap enough to
        run on *every* authenticated request without a DB round-trip.
        """
        from acn.services.agent_service import ALIVE_RENEW_TTL

        service = AgentService(mock_agent_repository)

        await service.touch_alive("test-agent-123")

        mock_agent_repository.set_alive.assert_called_once_with(
            "test-agent-123", ALIVE_RENEW_TTL
        )
        mock_agent_repository.find_by_id.assert_not_called()
        mock_agent_repository.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_touch_alive_swallows_repository_errors(self, mock_agent_repository):
        """``touch_alive`` runs as a fire-and-forget BackgroundTask after
        the response is sent — a Redis blip must NEVER propagate into the
        already-completed user-facing request that scheduled it.
        """
        mock_agent_repository.set_alive.side_effect = RuntimeError("redis down")

        service = AgentService(mock_agent_repository)

        # Must not raise.
        await service.touch_alive("test-agent-123")

        mock_agent_repository.set_alive.assert_called_once()

    @pytest.mark.asyncio
    async def test_join_subnet(self, mock_agent_repository, sample_agent):
        """Test agent joining a subnet"""
        # Setup mock
        mock_agent_repository.find_by_id.return_value = sample_agent

        service = AgentService(mock_agent_repository)

        agent = await service.join_subnet(sample_agent.agent_id, "new-subnet")

        assert "new-subnet" in agent.subnet_ids

        mock_agent_repository.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_leave_subnet(self, mock_agent_repository, sample_agent):
        """Test agent leaving a subnet"""
        # Setup mock - agent with multiple subnets
        sample_agent.subnet_ids = ["public", "subnet-1"]
        mock_agent_repository.find_by_id.return_value = sample_agent

        service = AgentService(mock_agent_repository)

        agent = await service.leave_subnet(sample_agent.agent_id, "subnet-1")

        assert "subnet-1" not in agent.subnet_ids
        assert "public" in agent.subnet_ids  # At least one remains

        mock_agent_repository.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_unregister_agent_success(self, mock_agent_repository, sample_agent):
        """Test unregistering an agent"""
        # Setup mock
        mock_agent_repository.find_by_id.return_value = sample_agent
        mock_agent_repository.delete.return_value = True

        service = AgentService(mock_agent_repository)

        success = await service.unregister_agent(
            sample_agent.agent_id,
            sample_agent.owner,
        )

        assert success is True

        mock_agent_repository.delete.assert_called_once_with(sample_agent.agent_id)

    @pytest.mark.asyncio
    async def test_unregister_agent_permission_denied(self, mock_agent_repository, sample_agent):
        """Test unregistering agent with wrong owner fails"""
        # Setup mock
        mock_agent_repository.find_by_id.return_value = sample_agent

        service = AgentService(mock_agent_repository)

        with pytest.raises(PermissionError):
            await service.unregister_agent(
                sample_agent.agent_id,
                "wrong-owner",  # Different owner
            )

    # ------------------------------------------------------------------
    # H1 — rotate_api_key
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_rotate_api_key_returns_plaintext_and_persists_hash(
        self, mock_agent_repository, sample_agent
    ):
        """rotate_api_key returns a fresh acn_* plaintext and stores only the hash."""
        from acn.services.agent_service import hash_api_key

        # The pre-rotation value can be either plaintext (legacy) or already
        # a hash — what matters is that rotation overwrites it with the new
        # hash, never with raw plaintext.
        sample_agent.api_key = "acn_OLD_PLAINTEXT_KEY"
        mock_agent_repository.find_by_id.return_value = sample_agent

        service = AgentService(mock_agent_repository)

        new_key = await service.rotate_api_key(sample_agent.agent_id)

        assert new_key.startswith("acn_"), "new key must keep the acn_ prefix"
        assert len(new_key) > len("acn_") + 32, "new key must carry ~256-bit entropy"
        # The stored value is the hash, not the plaintext. This is the
        # H1 invariant — even a full DB dump cannot replay the key.
        assert sample_agent.api_key == hash_api_key(new_key)
        assert sample_agent.api_key != new_key
        mock_agent_repository.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_rotate_api_key_each_call_produces_distinct_key(
        self, mock_agent_repository, sample_agent
    ):
        """Two consecutive rotations must produce two different plaintexts."""
        mock_agent_repository.find_by_id.return_value = sample_agent

        service = AgentService(mock_agent_repository)

        key1 = await service.rotate_api_key(sample_agent.agent_id)
        key2 = await service.rotate_api_key(sample_agent.agent_id)

        assert key1 != key2, "each rotation must mint fresh entropy"

    @pytest.mark.asyncio
    async def test_rotate_api_key_unknown_agent_raises(self, mock_agent_repository):
        """Rotating a non-existent agent surfaces AgentNotFoundException."""
        mock_agent_repository.find_by_id.return_value = None

        service = AgentService(mock_agent_repository)

        with pytest.raises(AgentNotFoundException):
            await service.rotate_api_key("agent-does-not-exist")

        mock_agent_repository.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_rotate_api_key_invalidates_lookup_by_old_key(
        self, mock_agent_repository, sample_agent
    ):
        """After rotation, get_agent_by_api_key(old_key) returns None.

        End-to-end H1 invariant: rotation is only useful if the previous
        credential genuinely stops working. We model the repository as a
        hash→agent index and verify the index no longer points back to
        the agent after the rotation save.
        """
        from acn.services.agent_service import hash_api_key

        # Build a tiny in-memory hash index against the agent.
        old_plaintext = "acn_OLD_PLAINTEXT_KEY"
        sample_agent.api_key = hash_api_key(old_plaintext)
        key_index: dict[str, object] = {sample_agent.api_key: sample_agent}

        async def fake_find_by_id(agent_id: str):
            return sample_agent if agent_id == sample_agent.agent_id else None

        async def fake_save(agent):
            # Drop any prior hash, install the new one. This is exactly
            # what a real repository must do after an in-place mutation.
            for h in list(key_index):
                if key_index[h] is agent:
                    del key_index[h]
            key_index[agent.api_key] = agent

        async def fake_find_by_api_key(key_hash: str):
            return key_index.get(key_hash)

        mock_agent_repository.find_by_id.side_effect = fake_find_by_id
        mock_agent_repository.save.side_effect = fake_save
        mock_agent_repository.find_by_api_key.side_effect = fake_find_by_api_key

        service = AgentService(mock_agent_repository)

        # Sanity: old key resolves before rotation.
        assert (
            await service.get_agent_by_api_key(old_plaintext)
        ).agent_id == sample_agent.agent_id

        new_key = await service.rotate_api_key(sample_agent.agent_id)

        # New key authenticates.
        assert (
            await service.get_agent_by_api_key(new_key)
        ).agent_id == sample_agent.agent_id
        # Old key no longer authenticates.
        assert await service.get_agent_by_api_key(old_plaintext) is None

    @pytest.mark.asyncio
    async def test_get_agent_by_api_key_no_longer_falls_back_to_legacy_lookup(
        self, mock_agent_repository
    ):
        """``get_agent_by_api_key`` must consult the hash index only.

        Pre-v0.12.0 there was a legacy plaintext fallback (and an
        auto-migrate save) for agents registered before API-key
        hashing. Once every live ``by_api_key`` index entry became a
        SHA-256 hash, the fallback turned into a perpetually cold
        branch — and a cold branch is exactly the kind of code that
        future refactors silently break. The interface method was
        removed; this pin catches anyone trying to bring it back
        without noticing.
        """
        mock_agent_repository.find_by_api_key.return_value = None

        service = AgentService(mock_agent_repository)
        result = await service.get_agent_by_api_key("acn_anything")

        assert result is None
        mock_agent_repository.find_by_api_key.assert_awaited_once()
        # Crucially: the legacy method must not exist on the
        # interface and must not have been called. AsyncMock would
        # auto-create attribute access into a coroutine on first
        # touch, masking the regression — so we assert against the
        # *spec* directly.
        assert not hasattr(type(mock_agent_repository), "find_by_api_key_legacy"), (
            "IAgentRepository.find_by_api_key_legacy was removed in v0.12.0; "
            "if you're re-adding it, update this test and revisit the "
            "rationale in services/agent_service.py::get_agent_by_api_key."
        )
