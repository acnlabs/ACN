"""Tests for BroadcastService ↔ PolicyRejected interaction.

Phase 1 review finding P1-1:
    BroadcastService (used by the A2A protocol's ``broadcast``
    action) treated PolicyRejected as a generic Exception. That
    was wrong on two axes:

    1.  **Per-target shape**: rejections produced
        ``{"error": <sanitized message>}`` — the same shape as a
        delivery failure (network / 5xx). API consumers couldn't
        distinguish "the recipient explicitly opted out" from
        "we couldn't reach them" without parsing strings.

    2.  **SEQUENTIAL contract**: ``_send_sequential`` breaks on the
        first exception. With PolicyRejected falling through, a
        single closed recipient in a sequential broadcast set would
        abort delivery to every subsequent target.

    The fix mirrors MessageService.broadcast_message: catch
    PolicyRejected explicitly, emit
    ``{"status": "rejected", "reason": ..., "reject_reason": ...}``
    (no ``error`` key), and **continue** the sequential loop.

These tests pin both axes plus the BroadcastResult.success counter
behaviour (rejected targets must not inflate the success count).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from a2a.compat.v0_3.types import Message, Role, TextPart

from acn.core.exceptions import PolicyRejected
from acn.infrastructure.messaging.broadcast_service import (
    BroadcastService,
    BroadcastStrategy,
)


def _make_message() -> Message:
    return Message(
        role=Role.user,
        message_id="msg-test",
        parts=[TextPart(text="hi")],
    )


def _make_router(*, side_effects_by_agent: dict | None = None):
    """Build a MessageRouter stub whose ``route`` returns or raises
    based on the target agent_id, so each test can express its
    intended fan-out outcome declaratively."""
    router = MagicMock()
    plan = side_effects_by_agent or {}

    async def route(from_agent, to_agent, message, **kwargs):
        outcome = plan.get(to_agent)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome or {"status": "delivered", "to": to_agent}

    router.route = route
    router.registry = MagicMock()
    return router


def _make_service(router) -> BroadcastService:
    """Build a BroadcastService with a stubbed redis log sink so
    tests don't pull in a real broker. ``_log_broadcast`` is
    fire-and-forget for the call sites under test."""
    redis_client = MagicMock()
    redis_client.zadd = AsyncMock()
    redis_client.expire = AsyncMock()
    svc = BroadcastService(router=router, redis_client=redis_client)
    svc._log_broadcast = AsyncMock()  # silence the audit hook
    return svc


# --------------------------------------------------------------------------- #
# Per-target shape consistency with MessageService
# --------------------------------------------------------------------------- #


class TestPerTargetRejectedShape:
    """The wire shape contract: clients of BroadcastService must see
    the same per-target dict for a rejection regardless of which
    strategy was selected. Otherwise consumers would have to switch
    on strategy when interpreting results — exactly the kind of
    cross-cutting coupling we want to avoid."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "strategy",
        [
            BroadcastStrategy.PARALLEL,
            BroadcastStrategy.SEQUENTIAL,
            BroadcastStrategy.BEST_EFFORT,
        ],
    )
    async def test_rejected_target_uses_status_field(self, strategy):
        rejection = PolicyRejected(
            recipient_id="closed-agent",
            reason="policy_closed",
            reject_reason="DND",
        )
        router = _make_router(side_effects_by_agent={"closed-agent": rejection})
        svc = _make_service(router)

        result = await svc.send(
            from_agent="sender",
            to_agents=["closed-agent"],
            message=_make_message(),
            strategy=strategy,
        )

        # Pinning the exact shape — drift here breaks API consumers.
        assert result.results["closed-agent"] == {
            "status": "rejected",
            "reason": "policy_closed",
            "reject_reason": "DND",
        }
        # ``error`` key is reserved for actual delivery failures.
        assert "error" not in result.results["closed-agent"]

    @pytest.mark.asyncio
    async def test_rejected_does_not_inflate_success_count(self):
        """A rejection is not a successful delivery. Pre-fix, the
        success counter incremented because there was no ``error``
        key, masking outright denied targets in dashboards."""
        rejection = PolicyRejected(
            recipient_id="closed",
            reason="policy_closed",
        )
        router = _make_router(side_effects_by_agent={"closed": rejection})
        svc = _make_service(router)

        result = await svc.send(
            from_agent="sender",
            to_agents=["closed", "ok"],
            message=_make_message(),
        )

        # Pinning the bucket-by-bucket count: 1 success (ok), 1 failed
        # (closed is rejected). Total reflects requested fan-out.
        assert result.total == 2
        assert result.success == 1
        assert result.failed == 1


# --------------------------------------------------------------------------- #
# SEQUENTIAL must continue past PolicyRejected — the actual security bug
# --------------------------------------------------------------------------- #


class TestSequentialDoesNotBreakOnRejection:
    """The original bug: in SEQUENTIAL mode, BroadcastService stops
    on the first exception. Pre-fix, a closed recipient at position
    [0] would skip every later target — silent denial-of-service.

    The contract we want: rejection ≠ delivery failure. Only real
    delivery failures (timeouts, 5xx, etc.) should trip the early-
    exit. Rejection is normal opt-out behaviour and must not impact
    the rest of the fan-out."""

    @pytest.mark.asyncio
    async def test_rejected_target_does_not_abort_sequential_loop(self):
        rejection = PolicyRejected(recipient_id="b", reason="policy_closed")
        router = _make_router(side_effects_by_agent={"b": rejection})
        svc = _make_service(router)

        result = await svc.send(
            from_agent="sender",
            to_agents=["a", "b", "c"],
            message=_make_message(),
            strategy=BroadcastStrategy.SEQUENTIAL,
        )

        # Pre-fix: results would have keys {"a", "b"} only — "c"
        # would be skipped because the loop broke on PolicyRejected.
        # Post-fix: all three keys present.
        assert set(result.results.keys()) == {"a", "b", "c"}
        assert result.results["a"]["status"] == "delivered"
        assert result.results["b"]["status"] == "rejected"
        assert result.results["c"]["status"] == "delivered"

    @pytest.mark.asyncio
    async def test_real_failure_still_breaks_sequential_loop(self):
        """Regression guard: the carve-out must be **specific to
        PolicyRejected**, not "skip all exceptions". A genuine
        upstream 500 / timeout still indicates a systemic issue
        and the historical SEQUENTIAL contract (stop, don't
        amplify) should hold."""
        router = _make_router(side_effects_by_agent={"b": RuntimeError("upstream 500")})
        svc = _make_service(router)

        result = await svc.send(
            from_agent="sender",
            to_agents=["a", "b", "c"],
            message=_make_message(),
            strategy=BroadcastStrategy.SEQUENTIAL,
        )

        # Loop breaks at "b" — "c" is never attempted.
        assert "a" in result.results
        assert "b" in result.results
        assert "c" not in result.results
        assert "error" in result.results["b"]


# --------------------------------------------------------------------------- #
# Mixed outcomes — pinning each kind of result coexists cleanly
# --------------------------------------------------------------------------- #


class TestNonDictResultRegression:
    """v2 review finding R5 — regression guard.

    ``router.route()`` returns an ``a2a.types.SendMessageResponse``
    Pydantic model when the target is online (the default happy
    path), not a dict. The original Phase 1 success-counter logic
    used ``"error" not in r``, which silently relied on Pydantic's
    default ``__contains__`` returning ``False`` to count online
    deliveries as success.

    My initial Phase 1 review fix tightened that to
    ``isinstance(r, dict) and "error" not in r and ...`` to exclude
    rejected dicts — but the ``isinstance`` gate also excluded
    SendMessageResponse, silently flipping the success/failed
    counts for every online recipient. This test pins the contract
    so that regression cannot recur: a non-dict result must count
    as success, and only dicts with explicit failure markers count
    as failed.
    """

    @pytest.mark.asyncio
    async def test_send_message_response_counts_as_success(self):
        """The realistic happy-path: real a2a SDK return type."""
        from a2a.compat.v0_3.types import SendMessageResponse

        # Construct a SendMessageResponse the same way the SDK does
        # for a successful message delivery. The exact body shape
        # doesn't matter to the counter — we just need a valid
        # Pydantic instance that is NOT a dict.
        real_response = SendMessageResponse(
            root={
                "jsonrpc": "2.0",
                "id": "x",
                "result": {
                    "kind": "message",
                    "role": "agent",
                    "message_id": "m",
                    "parts": [{"kind": "text", "text": "hi"}],
                },
            }
        )
        router = _make_router(side_effects_by_agent={"online": real_response})
        svc = _make_service(router)

        result = await svc.send(
            from_agent="sender",
            to_agents=["online"],
            message=_make_message(),
        )

        assert result.success == 1, (
            "online delivery returning SendMessageResponse must count "
            "as success — the implicit pre-Phase-1 invariant"
        )
        assert result.failed == 0
        # The result map preserves the raw response object so
        # downstream clients can inspect the SDK return value.
        assert result.results["online"] is real_response

    @pytest.mark.asyncio
    async def test_send_message_response_alongside_rejected(self):
        """Mixed: real SendMessageResponse for one target, policy
        rejection for another. Counters must bucket each correctly."""
        from a2a.compat.v0_3.types import SendMessageResponse

        real_response = SendMessageResponse(
            root={
                "jsonrpc": "2.0",
                "id": "x",
                "result": {
                    "kind": "message",
                    "role": "agent",
                    "message_id": "m",
                    "parts": [{"kind": "text", "text": "hi"}],
                },
            }
        )
        router = _make_router(
            side_effects_by_agent={
                "online": real_response,
                "closed": PolicyRejected(recipient_id="closed", reason="policy_closed"),
            }
        )
        svc = _make_service(router)

        result = await svc.send(
            from_agent="sender",
            to_agents=["online", "closed"],
            message=_make_message(),
        )

        assert result.success == 1
        assert result.failed == 1
        assert result.results["online"] is real_response
        assert result.results["closed"]["status"] == "rejected"


class TestMixedOutcomes:
    @pytest.mark.asyncio
    async def test_parallel_with_mixed_success_rejected_failed(self):
        """The realistic case: a fan-out of N targets where some
        succeed, some are policy-rejected, some have transient
        upstream issues. Each must end up in its own bucket without
        contaminating the others."""
        router = _make_router(
            side_effects_by_agent={
                "rejected-1": PolicyRejected(recipient_id="rejected-1", reason="policy_closed"),
                "failed-1": RuntimeError("network unreachable"),
            }
        )
        svc = _make_service(router)

        result = await svc.send(
            from_agent="sender",
            to_agents=["ok-1", "ok-2", "rejected-1", "failed-1"],
            message=_make_message(),
            strategy=BroadcastStrategy.PARALLEL,
        )

        assert result.total == 4
        assert result.success == 2  # ok-1, ok-2
        assert result.failed == 2   # rejected-1, failed-1
        assert result.results["rejected-1"]["status"] == "rejected"
        assert "error" in result.results["failed-1"]
        assert "error" not in result.results["rejected-1"]
