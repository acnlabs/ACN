"""Unit Tests for Subnet ``join_policy`` field (ADR-0004 Phase 1).

Pins the entity-layer invariants introduced by ADR-0004:

- ``join_policy`` defaults to ``"open"`` (legacy-compatible).
- ``join_policy`` must be one of ``{"open", "approval"}``.
- ``is_private=True`` requires ``join_policy="approval"`` — the
  status-quo ``private + open`` combination is rejected at
  construction time with a ``visibility_policy_conflict`` reason.
- ``to_dict`` / ``from_dict`` round-trip the field.
- ``from_dict`` auto-upgrades missing ``join_policy`` to
  ``"approval"`` when ``is_private`` is true (legacy compatibility
  with rows that predate ADR-0004 — keeps reads safe even before
  the Redis backfill script runs).

Service-layer invariants (the actual admission state machine that
reads ``join_policy``) live in
``tests/services/test_join_flow_service.py`` (one ``TestBranch{N}``
class per branch of the six-branch decision tree, plus
``TestStateMachineEdges`` and ``TestBranchOrderNormativity`` for
the cross-branch precedence rules) and are out of scope here.
"""

import pytest

from acn.core.entities.subnet import Subnet


class TestJoinPolicyDefault:
    """Legacy callers that never set ``join_policy`` get ``"open"``,
    matching pre-ADR-0004 behaviour for public subnets."""

    def test_default_is_open(self):
        subnet = Subnet(subnet_id="subnet-a", name="A", owner="agent-1")
        assert subnet.join_policy == "open"

    def test_public_subnet_with_explicit_open_accepted(self):
        # ``is_private=False`` (default) + ``join_policy="open"`` is the
        # status quo for public, freely-joinable subnets.
        subnet = Subnet(
            subnet_id="subnet-public",
            name="Public",
            owner="agent-1",
            is_private=False,
            join_policy="open",
        )
        assert subnet.join_policy == "open"
        assert subnet.is_private is False

    def test_public_subnet_with_approval_accepted(self):
        # Public + approval is one of the three legal combinations
        # introduced by ADR-0004 (public board with curated entry).
        subnet = Subnet(
            subnet_id="subnet-public-approval",
            name="Public-Approval",
            owner="agent-1",
            is_private=False,
            join_policy="approval",
        )
        assert subnet.join_policy == "approval"
        assert subnet.is_private is False


class TestJoinPolicyValueValidation:
    """``join_policy`` is typed ``Literal["open", "approval"]`` but
    ``Literal`` doesn't validate at runtime on a dataclass —
    ``__post_init__`` is the guard."""

    def test_unknown_value_rejected(self):
        with pytest.raises(ValueError, match="join_policy must be one of"):
            Subnet(
                subnet_id="subnet-a",
                name="A",
                owner="agent-1",
                join_policy="moderated",  # not in the legal set
            )

    def test_empty_string_rejected(self):
        # Falsy values should still fail validation, not silently
        # default — callers that want the default omit the kwarg.
        with pytest.raises(ValueError, match="join_policy must be one of"):
            Subnet(
                subnet_id="subnet-a",
                name="A",
                owner="agent-1",
                join_policy="",
            )


class TestVisibilityPolicyConflict:
    """ADR-0004's flagship invariant: ``is_private=True`` requires
    ``join_policy="approval"``. The combination ``private + open`` is
    the historical "private but joinable by anyone" semantic gap; the
    entity refuses to construct it so no service / route caller can
    smuggle the state into storage."""

    def test_private_plus_open_rejected(self):
        with pytest.raises(
            ValueError, match="visibility_policy_conflict"
        ):
            Subnet(
                subnet_id="subnet-priv",
                name="Priv",
                owner="agent-1",
                is_private=True,
                join_policy="open",
            )

    def test_private_plus_open_default_rejected(self):
        # ``join_policy`` defaults to ``"open"`` — so omitting it on a
        # private subnet must still trip the invariant. This is the
        # case real callers most likely hit and the one the ADR's
        # backfill targets.
        with pytest.raises(
            ValueError, match="visibility_policy_conflict"
        ):
            Subnet(
                subnet_id="subnet-priv",
                name="Priv",
                owner="agent-1",
                is_private=True,
            )

    def test_private_plus_approval_accepted(self):
        subnet = Subnet(
            subnet_id="subnet-priv",
            name="Priv",
            owner="agent-1",
            is_private=True,
            join_policy="approval",
        )
        assert subnet.is_private is True
        assert subnet.join_policy == "approval"


class TestJoinPolicyDictRoundTrip:
    """``to_dict`` / ``from_dict`` must round-trip the new field."""

    def test_to_dict_includes_join_policy(self):
        subnet = Subnet(
            subnet_id="subnet-a",
            name="A",
            owner="agent-1",
            is_private=True,
            join_policy="approval",
        )
        d = subnet.to_dict()
        assert d["join_policy"] == "approval"

    def test_from_dict_round_trips_join_policy(self):
        original = Subnet(
            subnet_id="subnet-a",
            name="A",
            owner="agent-1",
            is_private=True,
            join_policy="approval",
        )
        rebuilt = Subnet.from_dict(original.to_dict())
        assert rebuilt.join_policy == "approval"
        assert rebuilt.is_private is True

    def test_from_dict_tolerates_missing_join_policy_on_public_row(self):
        """Legacy public rows that predate ADR-0004 deserialise as
        ``join_policy="open"`` (the entity default). No auto-upgrade
        on these — they were always joinable, the field just records
        that fact explicitly going forward."""
        legacy_public = {
            "subnet_id": "subnet-legacy-public",
            "name": "Legacy",
            "owner": "agent-1",
            "is_private": False,
        }
        rebuilt = Subnet.from_dict(legacy_public)
        assert rebuilt.join_policy == "open"
        assert rebuilt.is_private is False


class TestFromDictLegacyAutoUpgrade:
    """ADR-0004 legacy compatibility: ``from_dict`` auto-upgrades
    missing ``join_policy`` to ``"approval"`` when ``is_private`` is
    true. This is the read-side equivalent of the Alembic backfill
    so deserialising a legacy private subnet doesn't trip the entity
    invariant before the Redis backfill script runs."""

    def test_legacy_private_row_auto_upgrades_to_approval(self):
        # Row predates ADR-0004 — no ``join_policy`` key at all.
        legacy_private = {
            "subnet_id": "subnet-legacy-priv",
            "name": "LegacyPriv",
            "owner": "agent-1",
            "is_private": True,
        }
        rebuilt = Subnet.from_dict(legacy_private)
        # Auto-upgraded — matches the Alembic backfill semantic.
        assert rebuilt.join_policy == "approval"
        assert rebuilt.is_private is True

    def test_explicit_join_policy_not_auto_upgraded(self):
        # If the caller explicitly passes ``join_policy="open"`` on a
        # private row, we honour the explicit value — and the entity
        # invariant rejects it. The auto-upgrade only kicks in when
        # the field is **absent**, never when it is present-but-wrong.
        explicit_conflict = {
            "subnet_id": "subnet-conflict",
            "name": "Conflict",
            "owner": "agent-1",
            "is_private": True,
            "join_policy": "open",
        }
        with pytest.raises(
            ValueError, match="visibility_policy_conflict"
        ):
            Subnet.from_dict(explicit_conflict)
