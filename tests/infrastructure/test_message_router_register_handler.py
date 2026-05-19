"""Regression tests for SCALE_AUDIT P2-2: MessageRouter.register_handler
used to blindly append handlers with no dedupe and no upper bound.

Behaviour after the fix:
  - Registering the same (message_type, handler) twice is an idempotent
    no-op — there is still exactly one entry.
  - Registering more than MAX_HANDLERS_PER_TYPE *distinct* handlers for
    one type raises ValueError.
  - unregister_handler() removes a specific registration and cleans up
    the bucket if it becomes empty.
"""

from unittest.mock import AsyncMock

import pytest

from acn.infrastructure.messaging.message_router import (
    MAX_HANDLERS_PER_TYPE,
    MessageRouter,
)


@pytest.fixture
def router():
    # Registry/redis aren't touched by register_handler, plain AsyncMocks
    # are enough — the handler registry is a pure in-memory dict.
    return MessageRouter(agent_service=AsyncMock(), redis_client=AsyncMock())


class TestDedupe:
    @pytest.mark.asyncio
    async def test_same_pair_registered_twice_is_a_noop(self, router: MessageRouter):
        async def h(_msg):
            return None

        await router.register_handler("task.started", h)
        await router.register_handler("task.started", h)

        assert router._handlers["task.started"] == [h], (
            "re-registering the same (type, handler) pair must not "
            "create duplicates — the bucket grows without bound otherwise"
        )

    @pytest.mark.asyncio
    async def test_distinct_handlers_for_same_type_both_kept(
        self, router: MessageRouter
    ):
        async def h1(_msg):
            return None

        async def h2(_msg):
            return None

        await router.register_handler("task.started", h1)
        await router.register_handler("task.started", h2)

        assert router._handlers["task.started"] == [h1, h2]


class TestCap:
    @pytest.mark.asyncio
    async def test_cap_allows_exactly_max_and_rejects_next(
        self, router: MessageRouter
    ):
        async def make_handler():
            async def _h(_msg):
                return None

            return _h

        handlers = [await make_handler() for _ in range(MAX_HANDLERS_PER_TYPE)]
        for h in handlers:
            await router.register_handler("task.started", h)

        assert len(router._handlers["task.started"]) == MAX_HANDLERS_PER_TYPE

        one_too_many = await make_handler()
        with pytest.raises(ValueError, match="handler cap reached"):
            await router.register_handler("task.started", one_too_many)

        assert len(router._handlers["task.started"]) == MAX_HANDLERS_PER_TYPE, (
            "the failed registration must leave the bucket untouched"
        )

    def test_cap_is_sane(self):
        """Don't let the cap silently drop to 0 or explode to millions."""
        assert 8 <= MAX_HANDLERS_PER_TYPE <= 1024, (
            f"MAX_HANDLERS_PER_TYPE={MAX_HANDLERS_PER_TYPE} is outside a "
            "plausible range"
        )


class TestUnregister:
    @pytest.mark.asyncio
    async def test_unregister_removes_specific_handler(self, router: MessageRouter):
        async def h1(_msg):
            return None

        async def h2(_msg):
            return None

        await router.register_handler("task.started", h1)
        await router.register_handler("task.started", h2)

        assert await router.unregister_handler("task.started", h1) is True
        assert router._handlers["task.started"] == [h2]

    @pytest.mark.asyncio
    async def test_unregister_empty_bucket_is_cleaned_up(self, router: MessageRouter):
        async def h(_msg):
            return None

        await router.register_handler("task.started", h)
        assert await router.unregister_handler("task.started", h) is True

        # Bucket should be gone entirely, not left as `[]` — otherwise
        # _handlers accumulates dead keys for every type ever used.
        assert "task.started" not in router._handlers

    @pytest.mark.asyncio
    async def test_unregister_unknown_returns_false(self, router: MessageRouter):
        async def h(_msg):
            return None

        assert await router.unregister_handler("never.registered", h) is False
