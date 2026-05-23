"""Unit Tests for Subnet Entity (ADR-0003 Phase 1)

Pins entity-layer invariants for the nesting fields shipped in
Phase 1. Single-layer cap and membership subset are enforced at
the *service* layer (Phase 2) so they live in
``tests/services/test_subnet_service_nesting.py`` rather than here.

Coverage strategy
-----------------
- Existing pre-nesting invariants (slug / name / owner non-empty,
  reserved-ID owner rule) are not repeated here — they're already
  covered by integration tests through the service / route layer.
  This file pins the *new* contracts introduced by ADR-0003.

- Each invariant gets one happy-path case and one or more violation
  cases. Construction-time ``ValueError`` is the contract; the
  message text is matched loosely with ``match=`` so refactors that
  rephrase the error don't trigger spurious test churn.
"""

import pytest

from acn.core.entities.subnet import Subnet


class TestSubnetNestingDefaults:
    """Default construction yields a top-level persistent subnet —
    the legacy semantics. This is the contract for callers that
    don't touch the new fields."""

    def test_defaults_to_top_level_persistent(self):
        subnet = Subnet(slug="subnet-a", name="A", owner="agent-1")

        assert subnet.parent_slug is None
        assert subnet.lifecycle == "persistent"
        assert subnet.linked_task_id is None

    def test_to_dict_round_trips_new_fields(self):
        subnet = Subnet(
            slug="subnet-child",
            name="Child",
            owner="agent-1",
            parent_slug="subnet-parent",
            lifecycle="task_scoped",
            linked_task_id="task-xyz",
        )

        d = subnet.to_dict()

        assert d["parent_slug"] == "subnet-parent"
        assert d["lifecycle"] == "task_scoped"
        assert d["linked_task_id"] == "task-xyz"

    def test_from_dict_tolerates_missing_nesting_keys(self):
        """Legacy stored rows predate ADR-0003 — their dict form
        lacks the three nesting keys entirely. ``from_dict`` must
        fall through to entity defaults rather than KeyError."""
        legacy_data = {
            "slug": "subnet-legacy",
            "name": "Legacy",
            "owner": "agent-1",
        }

        subnet = Subnet.from_dict(legacy_data)

        assert subnet.parent_slug is None
        assert subnet.lifecycle == "persistent"
        assert subnet.linked_task_id is None

    def test_from_dict_round_trips_new_fields(self):
        original = Subnet(
            slug="subnet-child",
            name="Child",
            owner="agent-1",
            parent_slug="subnet-parent",
            lifecycle="task_scoped",
            linked_task_id="task-xyz",
        )

        rebuilt = Subnet.from_dict(original.to_dict())

        assert rebuilt.parent_slug == "subnet-parent"
        assert rebuilt.lifecycle == "task_scoped"
        assert rebuilt.linked_task_id == "task-xyz"


class TestLifecycleValueValidation:
    """``lifecycle`` is typed ``Literal["persistent", "task_scoped"]``
    but Python's Literal does not validate at runtime on a
    dataclass. ``__post_init__`` is the guard."""

    def test_persistent_is_accepted(self):
        Subnet(
            slug="subnet-a",
            name="A",
            owner="agent-1",
            lifecycle="persistent",
        )

    def test_task_scoped_is_accepted_with_linked_task(self):
        Subnet(
            slug="subnet-a",
            name="A",
            owner="agent-1",
            lifecycle="task_scoped",
            linked_task_id="task-xyz",
        )

    def test_unknown_lifecycle_value_rejected(self):
        with pytest.raises(ValueError, match="lifecycle must be one of"):
            Subnet(
                slug="subnet-a",
                name="A",
                owner="agent-1",
                lifecycle="permanent",  # typo of "persistent"
            )

    def test_empty_lifecycle_rejected(self):
        # Falsy strings should still fail validation, not silently
        # default — callers that want the default omit the kwarg.
        with pytest.raises(ValueError, match="lifecycle must be one of"):
            Subnet(
                slug="subnet-a",
                name="A",
                owner="agent-1",
                lifecycle="",
            )


class TestTaskScopedLinkedTaskPairing:
    """``lifecycle == "task_scoped"`` ⇔ ``linked_task_id is not None``
    is enforced both directions so callers can't construct a
    half-state. This is the entity-layer guarantee Phase 3's
    cascade hook relies on."""

    def test_task_scoped_without_linked_task_rejected(self):
        with pytest.raises(
            ValueError, match="task_scoped.*requires linked_task_id"
        ):
            Subnet(
                slug="subnet-a",
                name="A",
                owner="agent-1",
                lifecycle="task_scoped",
                linked_task_id=None,
            )

    def test_persistent_with_linked_task_rejected(self):
        # The reverse direction — a "persistent" subnet shouldn't
        # carry a linked task. ``promote_to_persistent`` (Phase 2)
        # is responsible for clearing the field at the same time
        # as flipping the lifecycle; allowing one without the other
        # would let the by_linked_task index lie about what's
        # cascade-eligible.
        with pytest.raises(
            ValueError, match="persistent.*must not carry a linked_task_id"
        ):
            Subnet(
                slug="subnet-a",
                name="A",
                owner="agent-1",
                lifecycle="persistent",
                linked_task_id="task-leftover",
            )


class TestReservedSubnetNestingGuard:
    """Reserved subnets (``public`` / ``system``) can never become
    children — they're platform-owned with implicit "all agents"
    semantics that make the membership-subset invariant meaningless.
    The existing reserved-ID guard is extended to forbid
    ``parent_slug`` on reserved IDs."""

    def test_reserved_subnet_cannot_have_parent(self):
        # ``public`` must be owned by ``system`` (existing rule);
        # adding a parent on top should still be rejected.
        with pytest.raises(
            ValueError, match="Reserved subnet 'public' cannot have a parent_slug"
        ):
            Subnet(
                slug="public",
                name="Public",
                owner="system",
                parent_slug="some-parent",
            )

    def test_reserved_subnet_can_still_be_constructed_with_defaults(self):
        # Sanity: the new guard doesn't break the legacy "system
        # constructs reserved subnets at bootstrap" path.
        subnet = Subnet(slug="public", name="Public", owner="system")
        assert subnet.parent_slug is None
        assert subnet.lifecycle == "persistent"

    def test_reserved_subnet_cannot_be_task_scoped(self):
        # Reserved subnets are platform-owned with implicit "always-on"
        # semantics — a task_scoped lifecycle would let an arbitrary
        # task termination dissolve a platform subnet, breaking every
        # caller that assumes `public` is durable. Reject at
        # construction so we never persist the misshapen row.
        with pytest.raises(
            ValueError, match="Reserved subnet 'public' cannot be task_scoped"
        ):
            Subnet(
                slug="public",
                name="Public",
                owner="system",
                lifecycle="task_scoped",
                linked_task_id="task-evil",
            )
        with pytest.raises(
            ValueError, match="Reserved subnet 'system' cannot be task_scoped"
        ):
            Subnet(
                slug="system",
                name="System",
                owner="system",
                lifecycle="task_scoped",
                linked_task_id="task-evil",
            )


class TestSingleLayerCapNotEnforcedAtEntity:
    """ADR-0003 §A invariant 1 — single-layer cap — is intentionally
    a *service-layer* concern (it requires looking up the parent
    to check its own ``parent_slug``). The entity must accept
    a child pointing to any string ID without doing that lookup;
    Phase 2 tests in ``test_subnet_service_nesting.py`` pin the
    actual cap enforcement."""

    def test_entity_accepts_child_pointing_to_arbitrary_parent_id(self):
        # No parent existence check, no nesting depth check at the
        # entity layer. The entity is a passive data carrier here.
        subnet = Subnet(
            slug="subnet-grandchild",
            name="GC",
            owner="agent-1",
            parent_slug="subnet-some-parent",
        )
        assert subnet.parent_slug == "subnet-some-parent"


class TestFromDictLegacyKeyTranslation:
    """``Subnet.from_dict`` must accept dicts persisted before the
    ``subnet_id`` → ``slug`` rename so Redis-cached rows survive a
    rolling deploy without a backfill. Pinned here because mock-based
    repository tests don't exercise the real entity translation
    path — a regression here only surfaces on a real Redis deploy.
    """

    def test_translates_legacy_subnet_id_key_to_slug(self):
        subnet = Subnet.from_dict(
            {
                "subnet_id": "legacy-net",
                "name": "Legacy",
                "owner": "alice",
            }
        )

        assert subnet.slug == "legacy-net"

    def test_translates_legacy_parent_subnet_id_to_parent_slug(self):
        subnet = Subnet.from_dict(
            {
                "slug": "child-net",
                "name": "Child",
                "owner": "alice",
                "parent_subnet_id": "parent-net",
            }
        )

        assert subnet.parent_slug == "parent-net"

    def test_translates_both_legacy_keys_in_one_dict(self):
        # Realistic shape: a Redis HASH dumped before the rename
        # carries both legacy keys; from_dict must hydrate cleanly.
        subnet = Subnet.from_dict(
            {
                "subnet_id": "child-net",
                "name": "Child",
                "owner": "alice",
                "parent_subnet_id": "parent-net",
            }
        )

        assert subnet.slug == "child-net"
        assert subnet.parent_slug == "parent-net"

    def test_explicit_new_keys_take_precedence_over_legacy(self):
        # Defensive: if both keys appear (mid-rollout migration glitch),
        # the new ``slug`` / ``parent_slug`` win and the legacy values
        # are discarded rather than triggering a "duplicate keyword
        # argument" TypeError.
        subnet = Subnet.from_dict(
            {
                "slug": "new-slug",
                "subnet_id": "old-slug",
                "parent_slug": "new-parent",
                "parent_subnet_id": "old-parent",
                "name": "Mixed",
                "owner": "alice",
            }
        )

        assert subnet.slug == "new-slug"
        assert subnet.parent_slug == "new-parent"
