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

from acn.core.entities import Agent, Subnet
from acn.core.interfaces import IAgentRepository, IFollowRepository, ISubnetRepository


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


@pytest.fixture
def mock_follow_repository() -> IFollowRepository:
    """Mock FollowRepository for testing"""
    repo = AsyncMock(spec=IFollowRepository)
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


# ---------------------------------------------------------------------------
# Phase 2 PR #1: realistic Redis fixture for ManifestService tests
# ---------------------------------------------------------------------------
#
# The manifest queue uses ZADD/HSET/SET inside a MULTI/EXEC pipeline +
# PEXPIRE, ZRANGEBYSCORE with score ranges, and per-owner hash tags
# for cluster-slot affinity. ``AsyncMock(spec=redis.Redis)`` cannot
# reproduce those semantics — every command returns ``None`` unless
# explicitly stubbed, and the assertions we care about (atomicity,
# TTL bounds, summary truncation observed via real reads) need a
# Redis that actually executes the commands.
#
# We default to fakeredis (in-process, no server). When ``REDIS_URL``
# is set we use the real client so CI can opt into real-Redis runs by
# starting a sidecar. Either way the fixture is async, FLUSH'd before
# yielding, and disposed cleanly.
@pytest.fixture
async def manifest_redis() -> AsyncGenerator[redis.Redis, None]:
    """Async Redis-shaped client suitable for ManifestService testing.

    Prefers a real Redis (``REDIS_URL`` env var) when available; falls
    back to ``fakeredis.aioredis`` so contributors don't need a Redis
    daemon running locally. The real path matters in CI for
    catching cluster-mode regressions; the fake path is strictly
    faster on dev laptops.
    """
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        client: redis.Redis = redis.from_url(redis_url, decode_responses=False)
    else:
        # Imported lazily so contributors who don't run manifest
        # tests aren't forced to install fakeredis.
        from fakeredis import aioredis as _fakeredis_async

        client = _fakeredis_async.FakeRedis(decode_responses=False)

    # FLUSHDB to isolate from prior test runs. Fakeredis is per-
    # instance but we still call this so the real-Redis path doesn't
    # leak state between test files.
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


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
