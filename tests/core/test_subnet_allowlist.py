"""SubnetAllowlist entity contract tests (ADR-0004 Slice 2.1).

Thinner entity than ``SubnetJoinRequest`` — no state machine, just
identity invariants and the serialisation round-trip. The
non-empty ``added_by`` check is the one defence worth pinning
explicitly: an anonymous allowlist mutation destroys the audit
trail for the one privileged operation that lets an owner
preauth membership.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from acn.core.entities import SubnetAllowlist


def _now() -> datetime:
    return datetime.now(UTC)


class TestIdentityInvariants:
    @pytest.mark.parametrize(
        "field_name",
        ["slug", "agent_id", "added_by"],
    )
    def test_empty_identity_field_raises(self, field_name: str) -> None:
        kwargs: dict = {
            "slug": "s1",
            "agent_id": "a1",
            "added_by": "owner-1",
        }
        kwargs[field_name] = ""
        with pytest.raises(ValueError, match=f"{field_name} cannot be empty"):
            SubnetAllowlist(**kwargs)

    def test_canonical_construction_accepted(self) -> None:
        entry = SubnetAllowlist(
            slug="s1",
            agent_id="a1",
            added_by="owner-1",
        )
        assert entry.slug == "s1"
        assert entry.agent_id == "a1"
        assert entry.added_by == "owner-1"
        assert entry.added_at.tzinfo is not None  # default UTC-aware


class TestSerializationRoundTrip:
    def test_round_trip_preserves_all_fields(self) -> None:
        added_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        original = SubnetAllowlist(
            slug="s1",
            agent_id="a1",
            added_by="owner-1",
            added_at=added_at,
        )
        rebuilt = SubnetAllowlist.from_dict(original.to_dict())
        assert rebuilt == original

    def test_from_dict_requires_added_by(self) -> None:
        """Unlike ``SubnetJoinRequest`` — which has nullable fields
        and legacy-row tolerance — a row missing ``added_by`` is a
        corrupt record, not a backward-compat case. ``from_dict``
        surfaces the failure as ``KeyError`` immediately rather
        than silently defaulting."""
        with pytest.raises(KeyError):
            SubnetAllowlist.from_dict(
                {
                    "slug": "s1",
                    "agent_id": "a1",
                    # no added_by
                    "added_at": _now().isoformat(),
                }
            )
