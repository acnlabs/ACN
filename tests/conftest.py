"""Pytest Configuration and Fixtures

Shared fixtures for all tests.
"""

import asyncio
import os
from collections.abc import AsyncGenerator
from datetime import datetime
from unittest.mock import AsyncMock

# ---------------------------------------------------------------------------
# Test-time security defaults
# ---------------------------------------------------------------------------
# Settings.validate_security_settings rejects empty/short INTERNAL_API_TOKEN
# regardless of dev_mode (this is intentional — see C2 fix in the security
# audit). Tests don't get to set env vars after `from acn...` imports because
# Settings is built at import time, so we seed safe defaults here, before any
# acn.* import below.
os.environ.setdefault(
    "INTERNAL_API_TOKEN",
    "test-internal-token-must-be-at-least-32-characters-long",
)
os.environ.setdefault("DEV_MODE", "true")
os.environ.setdefault("HOST", "127.0.0.1")

import pytest
import redis.asyncio as redis

from acn.core.entities import Agent, AgentStatus, Subnet
from acn.core.interfaces import IAgentRepository, ISubnetRepository


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


# =============================================================================
# Mock Repositories
# =============================================================================


@pytest.fixture
def mock_agent_repository() -> IAgentRepository:
    """Mock AgentRepository for testing"""
    repo = AsyncMock(spec=IAgentRepository)
    return repo


@pytest.fixture
def mock_subnet_repository() -> ISubnetRepository:
    """Mock SubnetRepository for testing"""
    repo = AsyncMock(spec=ISubnetRepository)
    return repo


# =============================================================================
# Sample Entities
# =============================================================================


@pytest.fixture
def sample_agent() -> Agent:
    """Sample Agent entity for testing"""
    return Agent(
        agent_id="test-agent-123",
        owner="user-456",
        name="Test Agent",
        endpoint="https://agent.example.com",
        description="A test agent",
        tags=["task-planning", "code-generation"],
        subnet_ids=["public"],
        status=AgentStatus.ONLINE,
        metadata={"version": "1.0.0"},
        registered_at=datetime(2024, 1, 1, 12, 0, 0),
    )


@pytest.fixture
def sample_subnet() -> Subnet:
    """Sample Subnet entity for testing"""
    return Subnet(
        subnet_id="test-subnet-123",
        name="Test Subnet",
        owner="user-456",
        description="A test subnet",
        is_private=False,
        security_config={},
        metadata={},
        created_at=datetime(2024, 1, 1, 12, 0, 0),
    )


# =============================================================================
# Redis Mock
# =============================================================================


@pytest.fixture
async def mock_redis() -> AsyncGenerator[redis.Redis, None]:
    """Mock Redis client for testing"""
    mock = AsyncMock(spec=redis.Redis)

    # Setup common return values
    mock.hgetall.return_value = {}
    mock.smembers.return_value = set()
    mock.exists.return_value = 0

    yield mock


# =============================================================================
# FastAPI Test Client
# =============================================================================


@pytest.fixture
async def test_client():
    """FastAPI test client"""
    from httpx import AsyncClient

    from acn.api import app

    async with AsyncClient(app=app, base_url="http://test") as client:
        yield client
