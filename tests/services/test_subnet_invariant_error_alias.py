"""Pins the deprecated ``SubnetNestingError`` alias contract.

After the rename (``SubnetNestingError`` → ``SubnetInvariantError``)
the legacy name is re-exported via the module's ``__getattr__`` so
out-of-tree consumers — other agentplanet worktrees, downstream
SDKs, ad-hoc scripts — get a one-cycle migration window instead of
an ``ImportError`` on the next ``pip install``.

This test pins three properties of that alias so a future PR that
"cleans up" the ``__getattr__`` can't silently drop the migration
window:

1. The alias resolves to the same class object as
   ``SubnetInvariantError`` (so ``isinstance`` checks against either
   name still work cross-codebase).
2. Accessing the alias emits a ``DeprecationWarning`` with a stable
   pointer at the new name.
3. Accessing an unrelated nonexistent attribute still raises
   ``AttributeError`` — the ``__getattr__`` must be a targeted
   bridge, not a permissive catch-all that would silently swallow
   typos.
"""

from __future__ import annotations

import importlib
import warnings

import pytest

from acn.services import subnet_service


class TestSubnetNestingErrorAlias:
    def test_alias_resolves_to_same_class_as_new_name(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            legacy = subnet_service.SubnetNestingError

        assert legacy is subnet_service.SubnetInvariantError

    def test_alias_attribute_access_emits_deprecation_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _ = subnet_service.SubnetNestingError

        deprecations = [
            w for w in caught if issubclass(w.category, DeprecationWarning)
        ]
        assert deprecations, (
            "accessing SubnetNestingError must emit a DeprecationWarning"
        )
        msg = str(deprecations[0].message)
        assert "SubnetNestingError" in msg
        assert "SubnetInvariantError" in msg, (
            "warning must point callers at the new name"
        )

    def test_alias_import_emits_deprecation_warning(self):
        """``from acn.services.subnet_service import SubnetNestingError``
        — the most common downstream usage shape — also triggers the
        warning, not just attribute access."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            # ``module.SubnetNestingError`` exercises the same
            # ``__getattr__`` code path that ``from ... import
            # SubnetNestingError`` triggers internally (CPython
            # falls back to the module's ``__getattr__`` after the
            # primary lookup misses — PEP 562).
            mod = importlib.import_module("acn.services.subnet_service")
            _ = mod.SubnetNestingError  # type: ignore[attr-defined]

        assert any(
            issubclass(w.category, DeprecationWarning) for w in caught
        )

    def test_legacy_alias_raised_instance_isinstance_works_against_both_names(
        self,
    ):
        """The whole point of the alias: downstream code that does
        ``except SubnetNestingError`` keeps catching exceptions our
        service raises under the new name."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            LegacyName = subnet_service.SubnetNestingError

        raised = subnet_service.SubnetInvariantError("test_reason", "test msg")

        assert isinstance(raised, LegacyName)
        assert isinstance(raised, subnet_service.SubnetInvariantError)

    def test_unknown_attribute_still_raises_attributeerror(self):
        """``__getattr__`` is a targeted bridge — it must not turn
        every typo into a silent ``None``-equivalent."""
        with pytest.raises(AttributeError):
            _ = subnet_service.SubnetNestingErrorrr  # type: ignore[attr-defined]
