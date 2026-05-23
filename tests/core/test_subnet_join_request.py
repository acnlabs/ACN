"""SubnetJoinRequest entity contract tests (ADR-0004 Slice 2.1).

The entity exists primarily to enforce **construction-time
invariants** the persistence schema cannot express. These tests
pin every invariant explicitly so a future refactor that
"simplifies" ``__post_init__`` can't silently strip one — every
removed check is a silently-corrupted row that survives storage
and surfaces as a 500 at the next read.

Organisation mirrors the invariant set in
``SubnetJoinRequest.__post_init__``:

1. Identity fields non-empty.
2. ``kind`` and ``status`` membership in the literal sets.
3. (status, decided_by, decided_at) bidirectional coherence.
4. ``allowlist_auto`` shape: born approved by SYSTEM_ALLOWLIST_ACTOR.
5. ``note`` length cap.
6. ``to_dict`` / ``from_dict`` round-trip across all field shapes.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from acn.core.entities import SYSTEM_ALLOWLIST_ACTOR, SubnetJoinRequest


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# 1. Identity-field invariants
# ---------------------------------------------------------------------------


class TestIdentityFieldInvariants:
    @pytest.mark.parametrize(
        "field_name",
        ["request_id", "slug", "agent_id", "initiated_by"],
    )
    def test_empty_identity_field_raises(self, field_name: str) -> None:
        kwargs: dict = {
            "request_id": "r1",
            "slug": "s1",
            "agent_id": "a1",
            "kind": "join_request",
            "status": "pending",
            "initiated_by": "a1",
        }
        kwargs[field_name] = ""
        with pytest.raises(ValueError, match=f"{field_name} cannot be empty"):
            SubnetJoinRequest(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2. Discriminator + status enum membership
# ---------------------------------------------------------------------------


class TestKindAndStatusMembership:
    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError, match="kind must be one of"):
            SubnetJoinRequest(
                request_id="r1",
                slug="s1",
                agent_id="a1",
                kind="unknown_kind",  # type: ignore[arg-type]
                status="pending",
                initiated_by="a1",
            )

    def test_unknown_status_rejected(self) -> None:
        with pytest.raises(ValueError, match="status must be one of"):
            SubnetJoinRequest(
                request_id="r1",
                slug="s1",
                agent_id="a1",
                kind="join_request",
                status="banana",  # type: ignore[arg-type]
                initiated_by="a1",
            )


# ---------------------------------------------------------------------------
# 3. (status, decided_by, decided_at) bidirectional coherence
# ---------------------------------------------------------------------------


class TestDecisionCoherence:
    """The pending↔decided bidirectional check is THE invariant that
    keeps the audit trail honest. Strip it and you get phantom
    decisions on pending rows (corrupted reads) or undated approvals
    on terminal rows (corrupted state machine reconstruction)."""

    def test_pending_with_decided_by_rejected(self) -> None:
        with pytest.raises(ValueError, match="pending rows must have"):
            SubnetJoinRequest(
                request_id="r1",
                slug="s1",
                agent_id="a1",
                kind="join_request",
                status="pending",
                initiated_by="a1",
                decided_by="owner-1",
            )

    def test_pending_with_decided_at_rejected(self) -> None:
        with pytest.raises(ValueError, match="pending rows must have"):
            SubnetJoinRequest(
                request_id="r1",
                slug="s1",
                agent_id="a1",
                kind="join_request",
                status="pending",
                initiated_by="a1",
                decided_at=_now(),
            )

    def test_approved_without_decided_by_rejected(self) -> None:
        with pytest.raises(
            ValueError,
            match="status='approved' requires both decided_by and decided_at",
        ):
            SubnetJoinRequest(
                request_id="r1",
                slug="s1",
                agent_id="a1",
                kind="join_request",
                status="approved",
                initiated_by="a1",
                decided_at=_now(),  # decided_by missing
            )

    @pytest.mark.parametrize("status", ["rejected", "withdrawn"])
    def test_terminal_without_decided_at_rejected(self, status: str) -> None:
        with pytest.raises(ValueError, match=f"status={status!r} requires both"):
            SubnetJoinRequest(
                request_id="r1",
                slug="s1",
                agent_id="a1",
                kind="join_request",
                status=status,  # type: ignore[arg-type]
                initiated_by="a1",
                decided_by="owner-1",  # decided_at missing
            )

    def test_pending_with_no_decision_fields_accepted(self) -> None:
        r = SubnetJoinRequest(
            request_id="r1",
            slug="s1",
            agent_id="a1",
            kind="join_request",
            status="pending",
            initiated_by="a1",
        )
        assert r.is_pending is True
        assert r.is_terminal is False
        assert r.decided_by is None
        assert r.decided_at is None

    def test_approved_with_decision_fields_accepted(self) -> None:
        r = SubnetJoinRequest(
            request_id="r1",
            slug="s1",
            agent_id="a1",
            kind="join_request",
            status="approved",
            initiated_by="a1",
            decided_by="owner-1",
            decided_at=_now(),
        )
        assert r.is_pending is False
        assert r.is_terminal is True


# ---------------------------------------------------------------------------
# 4. allowlist_auto shape — born approved by SYSTEM_ALLOWLIST_ACTOR
# ---------------------------------------------------------------------------


class TestAllowlistAutoShape:
    """``allowlist_auto`` is the one kind without a pending lifecycle —
    born approved, decided_by=initiated_by=SYSTEM_ALLOWLIST_ACTOR.
    These checks defend against a future ``join`` route bug that
    materialises an ``allowlist_auto`` row with the applicant's
    agent_id as the actor — a record that would look like a
    self-approval and lie to every audit consumer."""

    def test_allowlist_auto_pending_rejected(self) -> None:
        with pytest.raises(
            ValueError, match="allowlist_auto rows must be born approved"
        ):
            SubnetJoinRequest(
                request_id="r1",
                slug="s1",
                agent_id="a1",
                kind="allowlist_auto",
                status="pending",
                initiated_by=SYSTEM_ALLOWLIST_ACTOR,
            )

    def test_allowlist_auto_with_wrong_initiated_by_rejected(self) -> None:
        with pytest.raises(
            ValueError, match="allowlist_auto rows must have initiated_by"
        ):
            SubnetJoinRequest(
                request_id="r1",
                slug="s1",
                agent_id="a1",
                kind="allowlist_auto",
                status="approved",
                initiated_by="owner-1",  # should be SYSTEM_ALLOWLIST_ACTOR
                decided_by=SYSTEM_ALLOWLIST_ACTOR,
                decided_at=_now(),
            )

    def test_allowlist_auto_with_wrong_decided_by_rejected(self) -> None:
        with pytest.raises(
            ValueError, match="allowlist_auto rows must have decided_by"
        ):
            SubnetJoinRequest(
                request_id="r1",
                slug="s1",
                agent_id="a1",
                kind="allowlist_auto",
                status="approved",
                initiated_by=SYSTEM_ALLOWLIST_ACTOR,
                decided_by="owner-1",  # should be SYSTEM_ALLOWLIST_ACTOR
                decided_at=_now(),
            )

    def test_canonical_allowlist_auto_accepted(self) -> None:
        r = SubnetJoinRequest(
            request_id="r1",
            slug="s1",
            agent_id="a1",
            kind="allowlist_auto",
            status="approved",
            initiated_by=SYSTEM_ALLOWLIST_ACTOR,
            decided_by=SYSTEM_ALLOWLIST_ACTOR,
            decided_at=_now(),
        )
        assert r.kind == "allowlist_auto"
        assert r.is_terminal is True
        assert r.initiated_by == SYSTEM_ALLOWLIST_ACTOR
        assert r.decided_by == SYSTEM_ALLOWLIST_ACTOR


# ---------------------------------------------------------------------------
# 5. Note length cap
# ---------------------------------------------------------------------------


class TestNoteLengthCap:
    def test_note_at_limit_accepted(self) -> None:
        r = SubnetJoinRequest(
            request_id="r1",
            slug="s1",
            agent_id="a1",
            kind="join_request",
            status="rejected",
            initiated_by="a1",
            decided_by="owner-1",
            decided_at=_now(),
            note="x" * 500,
        )
        assert r.note is not None
        assert len(r.note) == 500

    def test_note_over_limit_rejected(self) -> None:
        with pytest.raises(ValueError, match="note exceeds 500-char limit"):
            SubnetJoinRequest(
                request_id="r1",
                slug="s1",
                agent_id="a1",
                kind="join_request",
                status="rejected",
                initiated_by="a1",
                decided_by="owner-1",
                decided_at=_now(),
                note="x" * 501,
            )


# ---------------------------------------------------------------------------
# 6. to_dict / from_dict round-trip
# ---------------------------------------------------------------------------


class TestSerializationRoundTrip:
    """Round-trip discipline matters because Redis is a HASH and
    ``to_dict`` is the only point where every nullable field
    decides "empty string vs absent key". The reciprocal
    ``from_dict`` must collapse all our empty-string sentinels back
    to ``None`` so downstream readers don't see ``decided_by=''``
    instead of ``None``."""

    def test_pending_round_trip_preserves_none_decision_fields(self) -> None:
        original = SubnetJoinRequest(
            request_id="r1",
            slug="s1",
            agent_id="a1",
            kind="join_request",
            status="pending",
            initiated_by="a1",
        )
        rebuilt = SubnetJoinRequest.from_dict(original.to_dict())
        assert rebuilt.decided_by is None
        assert rebuilt.decided_at is None
        assert rebuilt.note is None
        assert rebuilt.status == "pending"

    def test_approved_round_trip_preserves_all_fields(self) -> None:
        decided_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        original = SubnetJoinRequest(
            request_id="r1",
            slug="s1",
            agent_id="a1",
            kind="invitation",
            status="approved",
            initiated_by="owner-1",
            decided_by="a1",
            decided_at=decided_at,
            note="welcome aboard",
        )
        rebuilt = SubnetJoinRequest.from_dict(original.to_dict())
        assert rebuilt == original

    def test_allowlist_auto_round_trip(self) -> None:
        original = SubnetJoinRequest(
            request_id="r1",
            slug="s1",
            agent_id="a1",
            kind="allowlist_auto",
            status="approved",
            initiated_by=SYSTEM_ALLOWLIST_ACTOR,
            decided_by=SYSTEM_ALLOWLIST_ACTOR,
            decided_at=_now(),
        )
        rebuilt = SubnetJoinRequest.from_dict(original.to_dict())
        assert rebuilt == original

    def test_to_dict_emits_empty_strings_for_none_nullable_fields(self) -> None:
        """Pins the wire format: nullable fields are NEVER omitted —
        they're serialised as empty strings. Drop this contract and
        a Redis ``HGETALL`` consumer that does ``.get("decided_by")``
        will start seeing ``None`` (missing key) instead of ``""``
        for pending rows, breaking every existing parser."""
        r = SubnetJoinRequest(
            request_id="r1",
            slug="s1",
            agent_id="a1",
            kind="join_request",
            status="pending",
            initiated_by="a1",
        )
        d = r.to_dict()
        assert d["decided_by"] == ""
        assert d["decided_at"] == ""
        assert d["note"] == ""
