"""Broadcast vs. PolicyRejected — per-target rejected semantics.

Phase 1 decision (see "Phase 1 网关执行点决策" in
docs/features/acn-communication-economic-model.md):

- Single-send: ``PolicyRejected`` propagates upward → routes layer
  maps it to HTTP 403 with structured detail.
- Broadcast: ``PolicyRejected`` is **never** raised out of the service.
  The fan-out continues; the rejected target is recorded as
  ``status: "rejected"`` in the per-target result list.

The contract pinned here is the *broadcast* half, which is the more
interesting one because it deliberately differs from the existing
``best_effort`` strategy — policy rejection skips even when
``strategy != "best_effort"``. Without the special-case, one closed
recipient would abort delivery to the rest of the fan-out set.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from a2a.types import Message, TextPart

from acn.core.exceptions import PolicyRejected
from acn.services.message_service import MessageService


def _make_msg() -> Message:
    return Message(
        message_id=str(uuid.uuid4()),
        role="user",
        parts=[TextPart(text="hello")],
    )


def _make_target(agent_id: str):
    target = MagicMock()
    target.agent_id = agent_id
    return target


def _make_svc_with_targets(target_ids: list[str], *, route_side_effect):
    """Build a MessageService whose repository returns the given target
    set and whose router.route applies ``route_side_effect`` per call.

    ``route_side_effect`` may be a list of values/exceptions matching
    the target order, or a callable receiving call kwargs.
    """
    sender = MagicMock()
    sender.agent_id = "agent-sender"

    targets = [_make_target(aid) for aid in target_ids]

    repo = MagicMock()

    async def find_by_id(aid: str):
        if aid == "agent-sender":
            return sender
        return None

    repo.find_by_id = AsyncMock(side_effect=find_by_id)
    repo.find_all = AsyncMock(return_value=[sender, *targets])
    repo.find_by_subnet = AsyncMock(return_value=targets)
    repo.find_by_tags = AsyncMock(return_value=targets)

    router_mock = MagicMock()
    router_mock.route = AsyncMock(side_effect=route_side_effect)

    return MessageService(router_mock, repo), router_mock


def _policy_rejected_for(agent_id: str) -> PolicyRejected:
    return PolicyRejected(
        reason="policy_closed",
        reject_reason=f"{agent_id} rejected reason",
        recipient_id=agent_id,
    )


@pytest.mark.asyncio
async def test_broadcast_records_per_target_rejected_status():
    """Closed recipient produces ``status: "rejected"`` with structured
    reason fields. Other targets in the same fan-out are unaffected."""
    svc, _router = _make_svc_with_targets(
        ["agent-open", "agent-closed", "agent-open-2"],
        route_side_effect=[
            {"message_id": "m1"},
            _policy_rejected_for("agent-closed"),
            {"message_id": "m3"},
        ],
    )

    responses = await svc.broadcast_message(
        from_agent_id="agent-sender",
        message=_make_msg(),
    )

    assert len(responses) == 3

    by_id = {r["agent_id"]: r for r in responses}
    assert by_id["agent-open"]["status"] == "success"
    assert by_id["agent-open-2"]["status"] == "success"

    rejected = by_id["agent-closed"]
    assert rejected["status"] == "rejected"
    assert rejected["reason"] == "policy_closed"
    assert rejected["reject_reason"] == "agent-closed rejected reason"


@pytest.mark.asyncio
async def test_broadcast_does_not_raise_even_with_strict_strategy():
    """The strict strategy (anything other than ``best_effort``) raises
    on generic delivery failures — but it must NOT raise on policy
    rejection. A closed recipient inside a broadcast set is a normal
    fan-out outcome, not a delivery error."""
    svc, _router = _make_svc_with_targets(
        ["agent-closed", "agent-open"],
        route_side_effect=[
            _policy_rejected_for("agent-closed"),
            {"message_id": "m2"},
        ],
    )

    # ``strategy="parallel"`` is the strict mode (raises on Exception).
    responses = await svc.broadcast_message(
        from_agent_id="agent-sender",
        message=_make_msg(),
        strategy="parallel",
    )

    assert len(responses) == 2
    by_id = {r["agent_id"]: r for r in responses}
    assert by_id["agent-closed"]["status"] == "rejected"
    assert by_id["agent-open"]["status"] == "success"


@pytest.mark.asyncio
async def test_broadcast_strict_still_raises_on_non_policy_failure():
    """Regression guard: special-casing ``PolicyRejected`` must NOT
    silently swallow other delivery failures. Network-style exceptions
    in strict mode still propagate."""
    svc, _router = _make_svc_with_targets(
        ["agent-flake"],
        route_side_effect=[ConnectionError("upstream gone")],
    )

    with pytest.raises(ConnectionError):
        await svc.broadcast_message(
            from_agent_id="agent-sender",
            message=_make_msg(),
            strategy="parallel",
        )


@pytest.mark.asyncio
async def test_broadcast_best_effort_still_records_failed_for_non_policy():
    """Regression guard for the existing ``best_effort`` semantics:
    network failures in best_effort still record ``status: "failed"``
    with sanitised error — the new ``rejected`` status is reserved
    exclusively for policy denials."""
    svc, _router = _make_svc_with_targets(
        ["agent-flake", "agent-ok"],
        route_side_effect=[
            ConnectionError("upstream gone"),
            {"message_id": "m2"},
        ],
    )

    responses = await svc.broadcast_message(
        from_agent_id="agent-sender",
        message=_make_msg(),
        strategy="best_effort",
    )

    by_id = {r["agent_id"]: r for r in responses}
    assert by_id["agent-flake"]["status"] == "failed"
    # Old per-target shape uses ``error`` not ``reason`` — the two
    # statuses must remain structurally distinguishable.
    assert "error" in by_id["agent-flake"]
    assert "reason" not in by_id["agent-flake"]
    assert by_id["agent-ok"]["status"] == "success"
