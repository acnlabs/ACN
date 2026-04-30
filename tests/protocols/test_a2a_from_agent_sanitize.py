"""Anti-spoofing tests for the A2A protocol entry's sender id.

Phase 1 review finding P0-2:
    PolicyCheckService grants an unconditional bypass to senders whose
    id starts with ``system:`` — the exemption assumes that anything
    in that namespace has already proven itself via
    ``X-Internal-Token`` + ``assert_system_caller``.

    The A2A protocol entry has neither gate. Pre-fix, any external
    caller could put ``"from_agent": "system:fake"`` in their A2A
    request metadata and bypass every closed recipient on the
    network — the most direct possible defeat of communication_policy.

    The fix introduces ``_safe_a2a_from_agent`` to demote any
    ``system:*`` value back to ``unknown`` before it reaches the
    policy gate.

These tests pin the demotion contract independently of the
PolicyCheckService internals, so even if the exemption rule's
implementation later changes the tests still catch a regression in
the sanitizer.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from acn.protocols.a2a.server import (
    _A2A_SAFE_FROM_AGENT_FALLBACK,
    _safe_a2a_from_agent,
)


def _ctx(metadata: dict | None) -> MagicMock:
    """Minimal RequestContext stand-in. Real RequestContext has many
    fields; the sanitizer only reads ``metadata`` (and uses the
    optional ``context_id`` / ``task_id`` for log attribution).
    """
    ctx = MagicMock()
    ctx.metadata = metadata
    ctx.context_id = "ctx-test"
    ctx.task_id = "task-test"
    return ctx


# --------------------------------------------------------------------------- #
# Demotion of system:* (the security-critical case)
# --------------------------------------------------------------------------- #


class TestSystemNamespaceIsDemoted:
    """Pinning every ``system:`` shape we could think of so a future
    refactor that 'helpfully' allowlists a specific subset (e.g.
    ``system:agentplanet-backend``) fails this test loudly. The
    demotion rule must remain unconditional at the A2A entry —
    legitimate system callers use ``/communication/internal/send``,
    which has its own token gate."""

    @pytest.mark.parametrize(
        "spoof",
        [
            "system:agentplanet-backend",
            "system:fake",
            "system:",  # empty slug — still a system: prefix
            "system:foo:bar",  # smuggled sub-namespace
            "system:" + "x" * 200,  # length doesn't excuse demotion
            "system:Foo123_Bar-Baz",
        ],
    )
    def test_system_prefix_collapses_to_unknown(self, spoof: str):
        ctx = _ctx({"from_agent": spoof})
        assert _safe_a2a_from_agent(ctx) == _A2A_SAFE_FROM_AGENT_FALLBACK

    def test_demotion_does_not_depend_on_metadata_having_other_keys(self):
        """Mirrors a real attack request: metadata contains *only*
        the spoofed from_agent — there's no other plausible content
        the sanitizer might key off."""
        ctx = _ctx({"from_agent": "system:evil"})
        assert _safe_a2a_from_agent(ctx) == "unknown"


# --------------------------------------------------------------------------- #
# Non-system values pass through (the regression-safety case)
# --------------------------------------------------------------------------- #


class TestNonSystemValuesPassThrough:
    """The sanitizer must not over-correct. Real agent ids (UUID4-shaped)
    and existing fallback string ``unknown`` must not be mutated, or
    every legitimate A2A broadcast / routing call would suddenly go
    through PolicyCheckService with a wrong sender label."""

    @pytest.mark.parametrize(
        "good",
        [
            "550e8400-e29b-41d4-a716-446655440000",
            "agent-foo",
            "unknown",
            "Systemic",  # contains "system" but not the prefix
            "Sys:fake",  # different prefix
            "user:abc",  # a different reserved-ish namespace, still allowed
            "x",  # minimal id
        ],
    )
    def test_non_system_value_returned_verbatim(self, good: str):
        ctx = _ctx({"from_agent": good})
        assert _safe_a2a_from_agent(ctx) == good


# --------------------------------------------------------------------------- #
# Edge cases on metadata shape — None, missing, wrong type
# --------------------------------------------------------------------------- #


class TestMetadataEdgeCases:
    def test_missing_from_agent_returns_fallback(self):
        ctx = _ctx({})
        assert _safe_a2a_from_agent(ctx) == _A2A_SAFE_FROM_AGENT_FALLBACK

    def test_metadata_none_returns_fallback(self):
        """RequestContext.metadata can be ``None`` (some A2A clients
        don't set it). Pre-fix this branch lived in a couple of
        places as ``context.metadata.get(...)`` which would raise
        AttributeError. The sanitizer must defend against it."""
        ctx = _ctx(None)
        assert _safe_a2a_from_agent(ctx) == _A2A_SAFE_FROM_AGENT_FALLBACK

    def test_no_metadata_attribute_at_all_returns_fallback(self):
        """Be defensive against future RequestContext shapes that drop
        the attribute entirely. ``getattr`` keeps the sanitizer
        forward-compatible without coupling to the SDK type."""
        ctx = MagicMock(spec=[])  # spec=[] strips MagicMock's auto-attr
        assert _safe_a2a_from_agent(ctx) == _A2A_SAFE_FROM_AGENT_FALLBACK

    def test_non_string_from_agent_returns_fallback(self):
        """Pydantic doesn't validate metadata values for us — clients
        could send numbers, lists, dicts, anything. Pinning that the
        sanitizer never returns a non-str so downstream
        ``str.startswith`` / metric labels can't blow up."""
        for bogus in [123, ["system:foo"], {"x": "y"}, None]:
            ctx = _ctx({"from_agent": bogus})
            assert _safe_a2a_from_agent(ctx) == _A2A_SAFE_FROM_AGENT_FALLBACK


# --------------------------------------------------------------------------- #
# Integration with PolicyCheckService — sanitizer + exemption together
# --------------------------------------------------------------------------- #


class TestSanitizerIntegratesWithPolicyExemption:
    """End-to-end behaviour: a client that puts ``system:*`` in their
    A2A metadata must NOT receive the policy exemption when they
    target a closed recipient. This is the actual attack we're
    defending against — the unit tests above pin the sanitizer in
    isolation, this one pins the composition."""

    def test_spoofed_system_is_rejected_by_closed_recipient(self):
        from acn.core.exceptions import PolicyRejected
        from acn.services.policy_service import PolicyCheckService

        sanitized = _safe_a2a_from_agent(
            _ctx({"from_agent": "system:agentplanet-backend"})
        )
        assert sanitized == "unknown"  # sanity

        svc = PolicyCheckService()
        with pytest.raises(PolicyRejected) as exc_info:
            svc.check_inbound_or_raise(
                sender_id=sanitized,
                recipient_id="agent-target",
                recipient_policy={"mode": "closed"},
            )
        # If this assertion fires with reason="policy_open"-like
        # text, the sanitizer regressed — the spoofed system: leaked
        # into the policy service.
        assert exc_info.value.reason == "policy_closed"
