"""Unit tests for PolicyCheckService.

Pins the Phase 1 + Phase 2 contract for gateway-level access control.
Every branch (open, closed, manifest, allowlist, unknown mode, system
exemption, raise wrapper) has a dedicated test so a regression in any
single rule is loud.

The service is fully type-decoupled — it accepts ``(sender_id,
recipient_id, recipient_policy_dict)`` rather than an Agent / AgentInfo
entity, plus optional kwargs (``message_meta``, ``is_in_allowlist``)
which is why the tests pass plain dicts and lambda stubs without any
fixture plumbing.

Phase 2 PR #2 made ``check_inbound`` / ``check_inbound_or_raise``
``async``: every test that calls them is now ``async def`` and is
covered by the project-wide ``asyncio_mode = auto`` pytest setting.

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


async def test_open_mode_allows():
    """Default ``open`` mode (the legacy behaviour every existing agent
    has) must allow any non-system sender — regression here would close
    the network the moment Phase 1 deploys.

    Phase 2 PR #1: ``route_to`` is now part of the contract and must be
    ``"inbox"`` for the open path so the router doesn't accidentally
    divert to manifest.
    """
    svc = PolicyCheckService()

    decision = await svc.check_inbound("sender-a", RECIPIENT_ID, {"mode": "open"})

    assert decision == PolicyDecision(allow=True, route_to="inbox")


async def test_none_policy_treated_as_open():
    """A ``None`` policy (e.g. a legacy AgentInfo that predates the
    Step 1 rollout, or an agent constructed without going through
    ``Agent.__post_init__``) must default to open — that's the
    backwards-compatibility guarantee the rollout depends on."""
    svc = PolicyCheckService()

    decision = await svc.check_inbound("sender-a", RECIPIENT_ID, None)

    assert decision.allow is True


async def test_empty_policy_treated_as_open():
    """An empty dict (defensive case — should not happen in practice
    because ``Agent.__post_init__`` backfills it, but the gateway must
    not KeyError if it ever does)."""
    svc = PolicyCheckService()

    decision = await svc.check_inbound("sender-a", RECIPIENT_ID, {})

    assert decision.allow is True


async def test_closed_mode_rejects_with_reason_code():
    """``closed`` mode must produce the structured short reason code
    ``policy_closed`` so HTTP layer + metric labels both have a stable
    string to match on."""
    svc = PolicyCheckService()

    decision = await svc.check_inbound("sender-a", RECIPIENT_ID, {"mode": "closed"})

    assert decision.allow is False
    assert decision.reason == "policy_closed"
    assert decision.reject_reason is None


async def test_closed_mode_passes_through_reject_reason():
    """The recipient's free-form ``reject_reason`` is the only piece
    of policy that's surfaced verbatim to the sender; the test pins the
    pass-through so a future refactor can't accidentally drop it."""
    svc = PolicyCheckService()
    policy = {"mode": "closed", "reject_reason": "On vacation until 2026-05"}

    decision = await svc.check_inbound("sender-a", RECIPIENT_ID, policy)

    assert decision.allow is False
    assert decision.reason == "policy_closed"
    assert decision.reject_reason == "On vacation until 2026-05"


async def test_system_sender_bypasses_closed_policy():
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

    decision = await svc.check_inbound("system:chat-backend", RECIPIENT_ID, policy)

    assert decision == PolicyDecision(allow=True)
    assert decision.route_to is None


async def test_unknown_mode_fails_closed():
    """Any mode outside the supported set is rejected (fail-closed).

    Phase 2 PR #2 expanded the supported set to ``{open, closed,
    manifest, allowlist}``. We pick a clearly-bogus typo to test the
    rejection branch (using the previous test's ``allowlist`` value
    no longer trips this branch).
    """
    svc = PolicyCheckService()

    decision = await svc.check_inbound(
        "sender-a", RECIPIENT_ID, {"mode": "definitely-not-a-real-mode"}
    )

    assert decision.allow is False
    assert decision.reason == "policy_unknown_mode"


async def test_system_sender_bypasses_even_unknown_mode():
    """System exemption short-circuits before mode parsing, so a
    misconfigured ``mode`` value still cannot break system traffic."""
    svc = PolicyCheckService()

    decision = await svc.check_inbound(
        "system:audit",
        RECIPIENT_ID,
        {"mode": "definitely-not-a-real-mode"},
    )

    assert decision.allow is True


async def test_message_meta_kwarg_is_keyword_only_and_currently_ignored():
    """``message_meta`` is reserved for Phase 3 extensions
    (fee_gated minimum). The test pins both that it's keyword-only (so
    positional callers can't accidentally couple to it) and that PR #2
    ignores its content."""
    svc = PolicyCheckService()

    # Keyword form works.
    assert (
        await svc.check_inbound(
            "sender-a",
            RECIPIENT_ID,
            {"mode": "open"},
            message_meta={"size_bytes": 99999},
        )
    ).allow is True

    # Positional form must not bind a fourth arg — guards against future
    # callers passing meta positionally and silently breaking when the
    # signature changes.
    with pytest.raises(TypeError):
        await svc.check_inbound(  # type: ignore[misc]
            "sender-a", RECIPIENT_ID, {"mode": "open"}, {"size_bytes": 1}
        )


# ---------------------------------------------------------------------------
# check_inbound_or_raise — short-circuit wrapper used by router / subnet mgr
# ---------------------------------------------------------------------------


async def test_or_raise_returns_silently_when_allowed():
    svc = PolicyCheckService()

    await svc.check_inbound_or_raise("sender-a", RECIPIENT_ID, {"mode": "open"})


async def test_or_raise_raises_policy_rejected_with_full_context():
    """The raised exception is the contract router/subnet handlers rely
    on to short-circuit. It must carry the recipient id (for audit),
    reason code (for HTTP/metric mapping) and reject_reason
    (for surfacing to the sender)."""
    svc = PolicyCheckService()
    policy = {"mode": "closed", "reject_reason": "Only tasks please"}

    with pytest.raises(PolicyRejected) as exc_info:
        await svc.check_inbound_or_raise("sender-a", RECIPIENT_ID, policy)

    err = exc_info.value
    assert err.reason == "policy_closed"
    assert err.reject_reason == "Only tasks please"
    assert err.recipient_id == RECIPIENT_ID
    assert "policy_closed" in str(err)
    assert "Only tasks please" in str(err)


async def test_or_raise_does_not_swallow_system_exemption():
    """Smoke test: the raise wrapper must agree with the decision
    method — system senders should never trigger the exception path."""
    svc = PolicyCheckService()

    await svc.check_inbound_or_raise(
        "system:notifier", RECIPIENT_ID, {"mode": "closed"}
    )


# ---------------------------------------------------------------------------
# Phase 2 PR #1 — manifest mode (accept-but-divert)
# ---------------------------------------------------------------------------


async def test_manifest_mode_allows_with_route_to_manifest():
    """``manifest`` mode is the accept-but-divert path. The decision
    must come back ``allow=True`` (so the router doesn't bump the
    rejection counter) but with ``route_to="manifest"`` so the router
    short-circuits to ManifestService.write instead of the inbox."""
    svc = PolicyCheckService()

    decision = await svc.check_inbound(
        "sender-a", RECIPIENT_ID, {"mode": "manifest"}
    )

    assert decision.allow is True
    assert decision.route_to == "manifest"
    assert decision.reason is None
    assert decision.reject_reason is None


async def test_manifest_mode_or_raise_does_not_raise():
    """``check_inbound_or_raise`` must NOT raise for manifest mode —
    the message is accepted from the sender's perspective; the divert
    happens in the router after the gate."""
    svc = PolicyCheckService()

    await svc.check_inbound_or_raise(
        "sender-a", RECIPIENT_ID, {"mode": "manifest"}
    )


async def test_system_sender_bypasses_manifest_mode():
    """System exemption applies uniformly — ``system:*`` senders bypass
    even manifest divert. Platform notifications must reach the inbox
    so user-facing UIs (chat mentions, payment alerts) keep working
    regardless of the recipient's manifest mode."""
    svc = PolicyCheckService()

    decision = await svc.check_inbound(
        "system:chat", RECIPIENT_ID, {"mode": "manifest"}
    )

    assert decision.allow is True
    assert decision.route_to is None


# ---------------------------------------------------------------------------
# Phase 2 PR #2 — allowlist mode (sender-conditional divert)
# ---------------------------------------------------------------------------
#
# Allowlist semantics:
#   - sender on the list → inbox
#   - sender NOT on list → manifest divert (NOT reject)
#   - empty list → manifest divert (graceful fresh-adopter UX)
#   - callback IO failure → manifest divert (P0-3 fail-closed)
#   - callback missing entirely → manifest divert (config error)
#   - system sender → bypass (uniform with closed/manifest)


def _allowlist_callback(members: dict[str, set[str]]):
    """Build a stub ``is_in_allowlist`` callable from a mapping.

    Mirrors the production shape ``(owner_id, target_id) -> bool``
    so tests pin the calling contract; the router will pass
    ``recipient`` first then ``sender`` matching this order.
    """

    async def _check(owner_id: str, target_id: str) -> bool:
        return target_id in members.get(owner_id, set())

    return _check


async def test_allowlist_member_routed_to_inbox():
    """A sender on the recipient's allowlist must reach the inbox —
    that's the entire point of the mode."""
    svc = PolicyCheckService()
    is_in = _allowlist_callback({RECIPIENT_ID: {"alice", "bob"}})

    decision = await svc.check_inbound(
        "alice",
        RECIPIENT_ID,
        {"mode": "allowlist"},
        is_in_allowlist=is_in,
    )

    assert decision == PolicyDecision(allow=True, route_to="inbox")


async def test_allowlist_non_member_diverts_to_manifest():
    """Non-members are diverted (not rejected) — graceful UX so a
    legitimate stranger's first message survives in the manifest queue
    instead of bouncing."""
    svc = PolicyCheckService()
    is_in = _allowlist_callback({RECIPIENT_ID: {"alice"}})

    decision = await svc.check_inbound(
        "stranger",
        RECIPIENT_ID,
        {"mode": "allowlist"},
        is_in_allowlist=is_in,
    )

    assert decision.allow is True
    assert decision.route_to == "manifest"
    assert decision.reason is None


async def test_allowlist_empty_diverts_everyone_to_manifest():
    """Fresh adopter case: the recipient flips to allowlist mode but
    hasn't added anyone yet. Every sender must divert to manifest —
    NOT reject (would deny-bomb the network on flip day) and NOT
    open (would defeat the security purpose)."""
    svc = PolicyCheckService()
    is_in = _allowlist_callback({RECIPIENT_ID: set()})

    decision = await svc.check_inbound(
        "stranger",
        RECIPIENT_ID,
        {"mode": "allowlist"},
        is_in_allowlist=is_in,
    )

    assert decision.allow is True
    assert decision.route_to == "manifest"


async def test_allowlist_callback_failure_diverts_to_manifest():
    """P0-3 fail-closed: when the callback raises (Redis blip, PG down)
    the policy service must NOT propagate the error and must NOT fail
    open. It diverts to manifest — preserves the message, blocks the
    inbox bypass."""
    svc = PolicyCheckService()

    async def _exploding(*_a, **_kw):
        raise RuntimeError("simulated Redis failure")

    decision = await svc.check_inbound(
        "alice",
        RECIPIENT_ID,
        {"mode": "allowlist"},
        is_in_allowlist=_exploding,
    )

    assert decision.allow is True
    assert decision.route_to == "manifest"


async def test_allowlist_missing_callback_diverts_to_manifest():
    """Configuration error path: the router didn't wire a callback
    (e.g. ``allowlist_service=None`` rollout opt-out) but the recipient
    has flipped their policy to allowlist anyway. Defensive default —
    divert to manifest so messages aren't lost while ops sees the
    error log."""
    svc = PolicyCheckService()

    decision = await svc.check_inbound(
        "alice",
        RECIPIENT_ID,
        {"mode": "allowlist"},
        # No is_in_allowlist passed.
    )

    assert decision.allow is True
    assert decision.route_to == "manifest"


async def test_system_sender_bypasses_allowlist_mode():
    """System exemption stays uniform — ``system:*`` traffic bypasses
    the allowlist gate just like it bypasses closed and manifest. The
    callback must NOT be called (it would be a needless IO and could
    even fail closed for a bug-causing config)."""
    svc = PolicyCheckService()
    called = False

    async def _check(*_a, **_kw):
        nonlocal called
        called = True
        return False

    decision = await svc.check_inbound(
        "system:chat",
        RECIPIENT_ID,
        {"mode": "allowlist"},
        is_in_allowlist=_check,
    )

    assert decision.allow is True
    assert decision.route_to is None
    assert called is False


async def test_or_raise_does_not_raise_for_allowlist_non_member():
    """Allowlist non-members divert (don't reject), so the raise
    wrapper must be a no-op for them — same shape as manifest mode."""
    svc = PolicyCheckService()
    is_in = _allowlist_callback({RECIPIENT_ID: set()})

    await svc.check_inbound_or_raise(
        "stranger",
        RECIPIENT_ID,
        {"mode": "allowlist"},
        is_in_allowlist=is_in,
    )


# ---------------------------------------------------------------------------
# Phase 2 PR #2 — validate_policy_dict expansion
# ---------------------------------------------------------------------------


def test_validate_policy_dict_accepts_manifest_mode():
    """Schema validator must let ``mode=manifest`` through so the
    PATCH /policy + register/join routes can persist it."""
    from acn.services.policy_service import validate_policy_dict

    out = validate_policy_dict({"mode": "manifest"})
    assert out == {"mode": "manifest"}

    out = validate_policy_dict({"mode": "manifest", "reject_reason": "Async only"})
    assert out == {"mode": "manifest", "reject_reason": "Async only"}


def test_validate_policy_dict_accepts_allowlist_mode():
    """Phase 2 PR #2 added ``allowlist`` to the supported set —
    the validator must accept it (members are stored in the
    ``agent_allowlist`` table, not in the policy dict)."""
    from acn.services.policy_service import validate_policy_dict

    out = validate_policy_dict({"mode": "allowlist"})
    assert out == {"mode": "allowlist"}

    out = validate_policy_dict(
        {"mode": "allowlist", "reject_reason": "By invitation only"}
    )
    assert out == {"mode": "allowlist", "reject_reason": "By invitation only"}


def test_validate_policy_dict_rejects_inline_allowlist_members():
    """Strict-keys: even though ``allowlist`` mode IS supported,
    putting members inline (``"allowlist": [...]``) must still be
    rejected. Members live in the relational table, not in the
    policy JSONB. This guards against drive-by additions that would
    activate on a future schema change."""
    from acn.services.policy_service import validate_policy_dict

    with pytest.raises(ValueError, match="unsupported key"):
        validate_policy_dict({"mode": "allowlist", "allowlist": ["alice"]})


def test_validate_policy_dict_rejects_unknown_mode():
    """Future-mode names must still be rejected at the schema layer
    — same as before PR #2 but anchored on a clearly-bogus value
    now that ``allowlist`` itself is accepted."""
    from acn.services.policy_service import validate_policy_dict

    with pytest.raises(ValueError, match="must be one of"):
        validate_policy_dict({"mode": "fee_gated"})
