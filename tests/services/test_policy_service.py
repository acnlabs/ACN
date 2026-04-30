"""Unit tests for PolicyCheckService.

Pins the Phase 1 contract for gateway-level access control. Every
branch (open, closed, unknown mode, system exemption, raise wrapper)
has a dedicated test so a regression in any single rule is loud.

The service is fully type-decoupled — it accepts ``(sender_id,
recipient_id, recipient_policy_dict)`` rather than an Agent / AgentInfo
entity, which is why the tests pass plain dicts without any fixture
plumbing.

See docs/features/acn-communication-economic-model.md
"Phase 1 网关执行点决策" for the design rationale these tests guard.
"""

import pytest

from acn.core.exceptions import PolicyRejected
from acn.services import PolicyCheckService, PolicyDecision

RECIPIENT_ID = "recipient-1"


# ---------------------------------------------------------------------------
# check_inbound — pure decision branches
# ---------------------------------------------------------------------------


def test_open_mode_allows():
    """Default ``open`` mode (the legacy behaviour every existing agent
    has) must allow any non-system sender — regression here would close
    the network the moment Phase 1 deploys.

    Phase 2 PR #1: ``route_to`` is now part of the contract and must be
    ``"inbox"`` for the open path so the router doesn't accidentally
    divert to manifest.
    """
    svc = PolicyCheckService()

    decision = svc.check_inbound("sender-a", RECIPIENT_ID, {"mode": "open"})

    assert decision == PolicyDecision(allow=True, route_to="inbox")


def test_none_policy_treated_as_open():
    """A ``None`` policy (e.g. a legacy AgentInfo that predates the
    Step 1 rollout, or an agent constructed without going through
    ``Agent.__post_init__``) must default to open — that's the
    backwards-compatibility guarantee the rollout depends on."""
    svc = PolicyCheckService()

    decision = svc.check_inbound("sender-a", RECIPIENT_ID, None)

    assert decision.allow is True


def test_empty_policy_treated_as_open():
    """An empty dict (defensive case — should not happen in practice
    because ``Agent.__post_init__`` backfills it, but the gateway must
    not KeyError if it ever does)."""
    svc = PolicyCheckService()

    decision = svc.check_inbound("sender-a", RECIPIENT_ID, {})

    assert decision.allow is True


def test_closed_mode_rejects_with_reason_code():
    """``closed`` mode must produce the structured short reason code
    ``policy_closed`` so HTTP layer + metric labels both have a stable
    string to match on."""
    svc = PolicyCheckService()

    decision = svc.check_inbound("sender-a", RECIPIENT_ID, {"mode": "closed"})

    assert decision.allow is False
    assert decision.reason == "policy_closed"
    assert decision.reject_reason is None


def test_closed_mode_passes_through_reject_reason():
    """The recipient's free-form ``reject_reason`` is the only piece
    of policy that's surfaced verbatim to the sender; the test pins the
    pass-through so a future refactor can't accidentally drop it."""
    svc = PolicyCheckService()
    policy = {"mode": "closed", "reject_reason": "On vacation until 2026-05"}

    decision = svc.check_inbound("sender-a", RECIPIENT_ID, policy)

    assert decision.allow is False
    assert decision.reason == "policy_closed"
    assert decision.reject_reason == "On vacation until 2026-05"


def test_system_sender_bypasses_closed_policy():
    """The ``system:*`` namespace is the single exemption rule. ACN's
    own internal channel (``POST /communication/internal/send``) must
    keep working even when the recipient is fully closed — otherwise
    chat-mention notifications and similar platform messages stop
    flowing the moment any agent toggles closed.

    System bypass leaves ``route_to`` unset (``None``) so the router
    falls through to its default inbox/HTTP path — system traffic is
    never diverted to manifest regardless of the recipient's mode.
    """
    svc = PolicyCheckService()
    policy = {"mode": "closed", "reject_reason": "busy"}

    decision = svc.check_inbound("system:chat-backend", RECIPIENT_ID, policy)

    assert decision == PolicyDecision(allow=True)
    assert decision.route_to is None


def test_unknown_mode_fails_closed():
    """Any mode outside the supported set is rejected (fail-closed).

    Phase 1 + Phase 2 PR #1 know three modes (``open``, ``closed``,
    ``manifest``); an unknown value almost always means a
    misconfiguration. Failing closed makes the typo loud (the owner's
    own test sends start failing immediately) instead of hiding the
    misconfiguration behind silent acceptance.
    """
    svc = PolicyCheckService()

    # ``allowlist`` is reserved for Phase 2 PR #2 and is intentionally
    # absent from the PR #1 supported set — a client built against a
    # future schema (``mode=allowlist``) hitting a current server must
    # be rejected loudly rather than silently degraded.
    decision = svc.check_inbound("sender-a", RECIPIENT_ID, {"mode": "allowlist"})

    assert decision.allow is False
    assert decision.reason == "policy_unknown_mode"


def test_system_sender_bypasses_even_unknown_mode():
    """System exemption short-circuits before mode parsing, so a
    misconfigured ``mode`` value still cannot break system traffic."""
    svc = PolicyCheckService()

    decision = svc.check_inbound(
        "system:audit",
        RECIPIENT_ID,
        {"mode": "definitely-not-a-real-mode"},
    )

    assert decision.allow is True


def test_message_meta_kwarg_is_keyword_only_and_currently_ignored():
    """``message_meta`` is reserved for Phase 2/3 extensions
    (manifest size threshold, fee_gated minimum). The test pins both
    that it's keyword-only (so positional callers can't accidentally
    couple to it) and that Phase 1 ignores its content."""
    svc = PolicyCheckService()

    # Keyword form works.
    assert svc.check_inbound(
        "sender-a",
        RECIPIENT_ID,
        {"mode": "open"},
        message_meta={"size_bytes": 99999},
    ).allow is True

    # Positional form must not bind a fourth arg — guards against future
    # callers passing meta positionally and silently breaking when the
    # signature changes.
    with pytest.raises(TypeError):
        svc.check_inbound(  # type: ignore[misc]
            "sender-a", RECIPIENT_ID, {"mode": "open"}, {"size_bytes": 1}
        )


# ---------------------------------------------------------------------------
# check_inbound_or_raise — short-circuit wrapper used by router / subnet mgr
# ---------------------------------------------------------------------------


def test_or_raise_returns_silently_when_allowed():
    svc = PolicyCheckService()

    # Must not raise.
    svc.check_inbound_or_raise("sender-a", RECIPIENT_ID, {"mode": "open"})


def test_or_raise_raises_policy_rejected_with_full_context():
    """The raised exception is the contract router/subnet handlers rely
    on to short-circuit. It must carry the recipient id (for audit),
    reason code (for HTTP/metric mapping) and reject_reason
    (for surfacing to the sender)."""
    svc = PolicyCheckService()
    policy = {"mode": "closed", "reject_reason": "Only tasks please"}

    with pytest.raises(PolicyRejected) as exc_info:
        svc.check_inbound_or_raise("sender-a", RECIPIENT_ID, policy)

    err = exc_info.value
    assert err.reason == "policy_closed"
    assert err.reject_reason == "Only tasks please"
    assert err.recipient_id == RECIPIENT_ID
    # __str__ contract: enough info for log lines without a custom formatter.
    assert "policy_closed" in str(err)
    assert "Only tasks please" in str(err)


def test_or_raise_does_not_swallow_system_exemption():
    """Smoke test: the raise wrapper must agree with the decision
    method — system senders should never trigger the exception path."""
    svc = PolicyCheckService()

    # Must not raise even when the recipient is closed.
    svc.check_inbound_or_raise(
        "system:notifier", RECIPIENT_ID, {"mode": "closed"}
    )


# ---------------------------------------------------------------------------
# Phase 2 PR #1 — manifest mode (accept-but-divert)
# ---------------------------------------------------------------------------


def test_manifest_mode_allows_with_route_to_manifest():
    """``manifest`` mode is the new accept-but-divert path. The decision
    must come back ``allow=True`` (so the router doesn't bump the
    rejection counter) but with ``route_to="manifest"`` so the router
    short-circuits to ManifestService.write instead of the inbox."""
    svc = PolicyCheckService()

    decision = svc.check_inbound("sender-a", RECIPIENT_ID, {"mode": "manifest"})

    assert decision.allow is True
    assert decision.route_to == "manifest"
    # No rejection metadata leaks into a manifest divert — these
    # fields are reserved for the closed-mode reject branch.
    assert decision.reason is None
    assert decision.reject_reason is None


def test_manifest_mode_or_raise_does_not_raise():
    """``check_inbound_or_raise`` must NOT raise for manifest mode —
    the message is accepted from the sender's perspective; the divert
    happens in the router after the gate."""
    svc = PolicyCheckService()

    # Must not raise; downstream router branch on route_to.
    svc.check_inbound_or_raise("sender-a", RECIPIENT_ID, {"mode": "manifest"})


def test_system_sender_bypasses_manifest_mode():
    """System exemption applies uniformly — ``system:*`` senders bypass
    even manifest divert. Platform notifications must reach the inbox
    so user-facing UIs (chat mentions, payment alerts) keep working
    regardless of the recipient's manifest mode."""
    svc = PolicyCheckService()

    decision = svc.check_inbound("system:chat", RECIPIENT_ID, {"mode": "manifest"})

    assert decision.allow is True
    # System bypass does NOT set route_to=manifest; the router will
    # fall through to its default inbox path.
    assert decision.route_to is None


# ---------------------------------------------------------------------------
# Phase 2 PR #1 — validate_policy_dict expansion
# ---------------------------------------------------------------------------


def test_validate_policy_dict_accepts_manifest_mode():
    """Schema validator must let ``mode=manifest`` through so the
    PATCH /policy + register/join routes can persist it."""
    from acn.services.policy_service import validate_policy_dict

    # Accepted with no extra fields.
    out = validate_policy_dict({"mode": "manifest"})
    assert out == {"mode": "manifest"}

    # ``reject_reason`` is reused as a free-form label for the manifest
    # listing UI (decision Group A #4).
    out = validate_policy_dict({"mode": "manifest", "reject_reason": "Async only"})
    assert out == {"mode": "manifest", "reject_reason": "Async only"}


def test_validate_policy_dict_rejects_allowlist_mode_in_pr1():
    """``allowlist`` mode is reserved for Phase 2 PR #2. PR #1 must
    reject it at the schema layer so users can't store half-baked
    allowlist policies that activate on PR #2 deploy."""
    from acn.services.policy_service import validate_policy_dict

    with pytest.raises(ValueError, match="must be one of"):
        validate_policy_dict({"mode": "allowlist"})
