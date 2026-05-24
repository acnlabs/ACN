"""Regression tests for SCALE_AUDIT P2-3: analytics.py used key patterns
that never matched the real schema.

Before the fix:
    - `scan_iter("acn:agents:*:info")` — suffix `:info` was never written.
       The real key is `acn:agents:{uuid}`.
    - `scan_iter("acn:subnets:*:info")` — the real key is
       `acn:subnets:info:{id}`.
    - `_count_agents_in_subnet` scanned every agent and filtered in Python;
       the authoritative source is already the set
       `acn:subnets:{slug}:agents`.

These made `get_agent_stats`, `get_subnet_stats`, and every health-check
consumer return permanently-zero data without raising any error.
"""

from unittest.mock import AsyncMock

import pytest

from acn.monitoring.analytics import Analytics


@pytest.fixture
def fake_redis():
    return AsyncMock()


@pytest.fixture
def analytics(fake_redis):
    return Analytics(fake_redis)


# =============================================================================
# get_agent_stats — scans the real hash keys only
# =============================================================================


class TestGetAgentStatsScansRealKeys:
    @pytest.mark.asyncio
    async def test_scans_acn_agents_without_info_suffix(
        self, analytics: Analytics, fake_redis: AsyncMock
    ):
        """The fix scans `acn:agents:*` (not `*:info`) and filters in-Python
        to exactly 3 colon-separated segments so index keys don't leak in."""

        async def _scan(pattern):
            # The caller must use the corrected pattern.
            assert pattern == "acn:agents:*", (
                f"analytics scans the wrong pattern {pattern!r}; "
                "must be `acn:agents:*`"
            )
            for k in [
                # Real agent hash keys (3 segments — should be kept).
                b"acn:agents:uuid-aaa",
                b"acn:agents:uuid-bbb",
                # Index / sidecar keys (4+ segments — should be filtered out).
                b"acn:agents:by_owner:user-1",
                b"acn:agents:by_endpoint:u:http://x",
                b"acn:agents:by_api_key:k",
                b"acn:agents:by_erc8004_id:1",
                b"acn:agents:uuid-aaa:alive",
            ]:
                yield k

        fake_redis.scan_iter = _scan
        fake_redis.hgetall = AsyncMock(return_value={})
        fake_redis.lrange = AsyncMock(return_value=[])

        stats = await analytics.get_agent_stats()

        # Only the 2 real agent hash keys should survive the segment filter.
        assert stats["total"] == 2, (
            "index keys like `by_owner` / `:alive` must not be counted as "
            f"agents; got total={stats['total']}"
        )

    @pytest.mark.asyncio
    async def test_does_not_scan_legacy_info_suffix(
        self, analytics: Analytics, fake_redis: AsyncMock
    ):
        """Anti-regression: legacy `acn:agents:*:info` pattern must be gone."""
        captured: list[str] = []

        async def _scan(pattern):
            captured.append(pattern)
            return
            yield  # pragma: no cover

        fake_redis.scan_iter = _scan
        fake_redis.lrange = AsyncMock(return_value=[])

        await analytics.get_agent_stats()

        assert "acn:agents:*:info" not in captured, (
            "legacy broken pattern must not be scanned anymore"
        )


# =============================================================================
# get_subnet_stats — uses the real `acn:subnets:info:*` pattern
# =============================================================================


class TestGetSubnetStatsScansRealKeys:
    @pytest.mark.asyncio
    async def test_scans_acn_subnets_info_prefix(
        self, analytics: Analytics, fake_redis: AsyncMock
    ):
        captured: list[str] = []

        async def _scan(pattern):
            captured.append(pattern)
            return
            yield  # pragma: no cover

        fake_redis.scan_iter = _scan
        # _count_agents_in_subnet reads from a set via scard.
        fake_redis.scard = AsyncMock(return_value=0)
        fake_redis.get = AsyncMock(return_value=None)

        await analytics.get_subnet_stats()

        assert "acn:subnets:info:*" in captured, (
            "must scan the real subnet schema; legacy `acn:subnets:*:info` "
            "never matched any key"
        )
        assert "acn:subnets:*:info" not in captured


# =============================================================================
# _count_agents_in_subnet — uses the membership set, not a full scan
# =============================================================================


class TestCountAgentsInSubnetUsesSet:
    @pytest.mark.asyncio
    async def test_reads_scard_of_membership_set(
        self, analytics: Analytics, fake_redis: AsyncMock
    ):
        fake_redis.scard = AsyncMock(return_value=42)

        count = await analytics._count_agents_in_subnet("public")

        fake_redis.scard.assert_awaited_once_with("acn:subnets:public:agents")
        assert count == 42

    @pytest.mark.asyncio
    async def test_does_not_scan_all_agents(
        self, analytics: Analytics, fake_redis: AsyncMock
    ):
        """Anti-regression: the O(N_agents) scan+filter path is gone."""
        fake_redis.scard = AsyncMock(return_value=0)
        # If the function tries to scan, this will throw — which is what we
        # want: `scard` is the only allowed Redis call here.
        fake_redis.scan_iter = AsyncMock(
            side_effect=AssertionError(
                "_count_agents_in_subnet must not scan; it must read the "
                "authoritative `acn:subnets:{id}:agents` set via scard"
            )
        )

        await analytics._count_agents_in_subnet("public")
