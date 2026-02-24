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
            skills=["task-planning"],
        )

        # Verify agent created
        assert agent.owner == "user-123"
        assert agent.name == "Test Agent"
        assert agent.endpoint == "https://agent.example.com"
        assert agent.skills == ["task-planning"]
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
            skills=["new-skill"],
        )

        # Verify agent updated
        assert agent.agent_id == sample_agent.agent_id  # Same ID
        assert agent.name == "Updated Name"  # Updated
        assert agent.skills == ["new-skill"]  # Updated
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
        mock_agent_repository.find_by_skills.return_value = [sample_agent]
        mock_agent_repository.filter_alive.return_value = {sample_agent.agent_id}

        service = AgentService(mock_agent_repository)

        agents = await service.search_agents(
            skills=["task-planning"],
            status="online",
        )

        assert len(agents) == 1
        assert agents[0].agent_id == sample_agent.agent_id

        mock_agent_repository.find_by_skills.assert_called_once_with(["task-planning"], "online")

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
