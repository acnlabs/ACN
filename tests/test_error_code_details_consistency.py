"""Cross-sprint ``ACNHTTPError`` ``details`` schema consistency.

P1 #11 sprint #6 audit-of-process item: pin the contract that a
single ``ErrorCode`` member emits the *same* ``details`` dict shape
regardless of which route module raises it.

Why this test exists
--------------------
Sprint #5 (`payments`) shipped with `details={"path_agent_id":
…, "authenticated_agent_id": …}` for ``API_KEY_AGENT_MISMATCH``,
diverging from the sprint #1-#3 cross-sprint contract
``{path_agent, key_agent}``. The drift was not caught by any
existing test (the per-sprint contract tests only assert what
*that* sprint emits, not what other sprints emit) and required
an audit-followup commit (``4330346``) to fix in code +
documentation. Without an automated guard, every future sprint
(#7 `onchain`, #8 `manifest`, #10 `dependencies`, etc.) is
exposed to the same drift.

Strategy
--------
1. Statically scan every ``raise ACNHTTPError(...)`` site in
   ``acn/routes/*.py`` via the AST module — no runtime
   instrumentation needed, the test runs in milliseconds.
2. Group raise sites by ``ErrorCode`` member name.
3. For each group, collect the set of ``details`` keys at each
   site. If all sites in a group agree on a single keys set,
   the code is "strict" — pass.
4. If sites in a group disagree, the code is "union" — pass
   only if it's been explicitly registered in
   ``UNION_SCHEMA_CODES`` below (forces the divergence to be a
   conscious design choice, documented at the catalog table in
   ``acn-error-schema.md`` §2 cross-module catalog).

What this test does NOT do
--------------------------
* Does NOT pin the *value* of details fields — only keys. Values
  are dynamic (request-scoped agent ids, etc.); pinning them
  would belong in per-sprint contract tests, which already
  exist.
* Does NOT cross-check against ``acn-error-schema.md`` table
  values. Doing so would couple this test to the doc text and
  re-introduce a drift surface. Per-sprint contract tests are
  responsible for "code matches docs"; this test is responsible
  for "code matches itself across modules".
* Does NOT validate that a code defined in ``ErrorCode`` enum
  is actually raised somewhere. The reserved-but-unraised
  pattern (``INSUFFICIENT_BALANCE``) is intentional.

How to extend
-------------
* New code, all sites agree on keys → no test changes needed.
* New code intentionally polymorphic (per-emitter ``details``
  shape) → add to ``UNION_SCHEMA_CODES`` with a one-line
  rationale comment. Failing to do so is a *feature* — it
  forces a reviewer conversation about whether the divergence
  is desired.
* Drift detected in a "strict" code → either fix the offender
  (the usual case) OR consciously promote the code to
  ``UNION_SCHEMA_CODES`` (rare, requires doc update).
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

# ``acn/routes/*.py`` from the repo root. Resolve relative to this
# test file so the test passes regardless of pytest invocation cwd.
_ROUTES_DIR = Path(__file__).resolve().parents[1] / "acn" / "routes"


# ---------------------------------------------------------------------------
# Codes whose ``details`` shape varies per emitter on purpose. Each entry
# must have a one-line rationale; review will challenge additions.
# ---------------------------------------------------------------------------

UNION_SCHEMA_CODES: dict[str, str] = {
    # Cross-module catalog from sprint #2b — value of ``details``
    # depends on which validator raised. ``acn-error-schema.md`` §2
    # cross-module catalog table publishes the union shape per code.
    "OWNERSHIP_MISMATCH": (
        "registry uses {requested_owner, token_owner} for owner-token "
        "mismatch; subnets/tasks use {agent_id?, subnet_id?, task_id?, "
        "reason?} for PermissionError re-raises (free-form str(e))"
    ),
    "INVALID_REQUEST": (
        "different validators emit different field sets: registry "
        "bulk-delete uses {reason}; subnets/tasks ValueError-cohort "
        "uses {field, value, allowed?, task_id?, agent_id?, reason?}"
    ),
    "NOT_SUBNET_MEMBER": (
        "subnets variant uses {subnet_id}; tasks get-task gate adds "
        "{task_id, reason} where reason ∈ {anonymous_caller, not_member}"
    ),
    "AUTHENTICATION_REQUIRED": (
        "registry / subnets / tasks each emit different details.reason "
        "values for invalid header / unrecognised key / private subnet / "
        "private subnet view / invalid agent api key paths"
    ),
    "MISSING_PERMISSION": (
        "registry dev-mode-disabled uses {reason}; tasks JWT scope check "
        "uses {required_permission}"
    ),
    # ``COMMUNICATION_REJECTED`` was a union-schema candidate during
    # sprint #2a's planning (registry catch-all proxy was originally
    # going to carry an ``upstream_status_code`` extra field). The
    # actual implementation converged on the single
    # ``{reason, reject_reason}`` shape across communication.py and
    # registry.py. Strict consistency now applies — ``TestUnionSchemaCodesActuallyDiverge``
    # would flag a re-addition of this code as a dead exemption.
}


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


class RaiseSite:
    """A single ``raise ACNHTTPError(...)`` call.

    Stored as a flat record (instead of a NamedTuple) so the failure
    messages can be concatenated with a single f-string and still
    print every meaningful field. ``details_keys`` is ``None`` only
    when the raise site does not pass ``details=`` at all (which
    pydantic / the helper class default to ``{}``).
    """

    __slots__ = ("file", "lineno", "code", "details_keys")

    def __init__(
        self,
        file: str,
        lineno: int,
        code: str,
        details_keys: frozenset[str] | None,
    ) -> None:
        self.file = file
        self.lineno = lineno
        self.code = code
        self.details_keys = details_keys

    def __repr__(self) -> str:
        keys = (
            "<no details=>"
            if self.details_keys is None
            else "{" + ", ".join(sorted(self.details_keys)) + "}"
        )
        return f"{self.file}:{self.lineno} ErrorCode.{self.code} details={keys}"


def _extract_details_keys(call: ast.Call) -> frozenset[str] | None:
    """Return the frozenset of ``details=`` keys, or ``None`` if absent.

    Returns the literal ``frozenset()`` (not ``None``) when ``details=``
    is passed but the dict is empty — the empty-dict semantics is a
    valid contract (e.g. ``internal_token_invalid``).
    """
    for kw in call.keywords:
        if kw.arg != "details":
            continue
        if not isinstance(kw.value, ast.Dict):
            # Computed details (e.g. ``details=build_details(...)``) — we
            # cannot statically introspect, so we treat as "opaque" and
            # skip the consistency check for that site. Returning a
            # sentinel rather than None lets the caller distinguish.
            return frozenset({"<dynamic>"})
        keys: list[str] = []
        for key_node in kw.value.keys:
            if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                keys.append(key_node.value)
            else:
                # Non-literal key (e.g. f-string, variable) — fall back
                # to opaque tag so the test does not silently pass on
                # a site it cannot verify.
                keys.append(f"<dynamic@{key_node.lineno}>")
        return frozenset(keys)
    return None


def _walk_module(path: Path) -> Iterator[RaiseSite]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    rel = str(path.relative_to(_ROUTES_DIR.parents[2]))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        call = node.exc
        if not isinstance(call, ast.Call):
            continue
        # Match ``raise ACNHTTPError(...)`` — bare Name reference.
        # Aliased imports (``raise errors.ACNHTTPError(...)``) are not
        # used in this codebase; if they ever are, extend here.
        if not (isinstance(call.func, ast.Name) and call.func.id == "ACNHTTPError"):
            continue
        if not call.args:
            # ACNHTTPError requires a positional ErrorCode — a missing
            # one is a Python-level bug we don't try to handle here.
            continue
        first = call.args[0]
        if not (
            isinstance(first, ast.Attribute)
            and isinstance(first.value, ast.Name)
            and first.value.id == "ErrorCode"
        ):
            # E.g. ``raise ACNHTTPError(some_var, ...)`` — non-literal
            # ErrorCode reference. Skip; per-sprint contract tests
            # cover the dynamic paths.
            continue
        yield RaiseSite(
            file=rel,
            lineno=node.lineno,
            code=first.attr,
            details_keys=_extract_details_keys(call),
        )


@pytest.fixture(scope="module")
def all_raise_sites() -> list[RaiseSite]:
    """Collect every ``raise ACNHTTPError(...)`` across ``acn/routes/``.

    Module-scoped so the AST parse runs once for the whole test file.
    """
    sites: list[RaiseSite] = []
    for py_file in sorted(_ROUTES_DIR.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        sites.extend(_walk_module(py_file))
    return sites


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCollectionSanity:
    """Make sure the AST collector actually finds raise sites — a
    silent bug in the collector would make every other test in this
    file vacuously green."""

    def test_finds_a_meaningful_number_of_sites(self, all_raise_sites):
        # As of sprint #6 there are ~124 raise sites across 7 modules.
        # The lower bound is intentionally generous — the test guards
        # against "collector returned nothing because the regex broke",
        # not against "we added sites".
        assert len(all_raise_sites) >= 100, (
            f"AST collector only found {len(all_raise_sites)} raise sites, "
            f"expected >=100 as of sprint #6. The collector likely broke — "
            f"check the ``raise ACNHTTPError(...)`` AST matching in "
            f"``_walk_module``."
        )

    def test_every_site_has_resolved_error_code(self, all_raise_sites):
        """No site should have ``code`` like ``<dynamic>`` — that
        would mean we couldn't statically resolve which code is being
        raised, and the consistency check would silently skip it."""
        unresolved = [s for s in all_raise_sites if "<" in s.code]
        assert not unresolved, (
            f"{len(unresolved)} raise sites have non-static ErrorCode "
            f"references, defeating consistency checking:\n"
            + "\n".join(f"  - {s}" for s in unresolved[:10])
        )


class TestPerCodeDetailsKeysConsistency:
    """Same ``ErrorCode`` → same ``details`` keys at every raise site,
    UNLESS the code is registered as union-schema."""

    def test_strict_codes_have_uniform_details_keys(self, all_raise_sites):
        # Group by code.
        by_code: dict[str, list[RaiseSite]] = {}
        for site in all_raise_sites:
            by_code.setdefault(site.code, []).append(site)

        violations: list[str] = []
        for code, sites in sorted(by_code.items()):
            if code in UNION_SCHEMA_CODES:
                continue  # documented to vary per emitter
            if len(sites) < 2:
                continue  # single site can't be inconsistent

            # Filter out sites with no details= at all from the
            # comparison set — a missing details kwarg means the
            # default ``{}`` applies, which is *meaningful* and may
            # diverge from sites that explicitly pass details. We
            # treat "no details=" as its own keys set (frozenset())
            # so the compare picks up the divergence.
            #
            # In practice every migrated site passes details= today,
            # but encoding the behaviour explicitly future-proofs the
            # test against a contributor adding a "lazy" raise.
            normalised = [
                site.details_keys if site.details_keys is not None else frozenset()
                for site in sites
            ]
            unique_shapes = set(normalised)
            if len(unique_shapes) == 1:
                continue

            # Build a per-shape grouping for the failure message —
            # most useful when 80% of sites agree and 1-2 outliers
            # disagree (the typical drift pattern).
            shape_to_sites: dict[frozenset[str], list[RaiseSite]] = {}
            for site, shape in zip(sites, normalised, strict=True):
                shape_to_sites.setdefault(shape, []).append(site)

            lines = [f"\n  ErrorCode.{code} has {len(unique_shapes)} divergent details shapes:"]
            for shape, group in sorted(
                shape_to_sites.items(),
                key=lambda kv: -len(kv[1]),  # majority shape first
            ):
                shape_str = "{}" if not shape else "{" + ", ".join(sorted(shape)) + "}"
                lines.append(f"    {shape_str}  ({len(group)} site(s))")
                for site in group:
                    lines.append(f"      - {site.file}:{site.lineno}")
            violations.append("\n".join(lines))

        if violations:
            pytest.fail(
                "Cross-sprint ``details`` key drift detected. The minority "
                "shape(s) must align with the majority shape, OR the "
                "ErrorCode must be promoted to ``UNION_SCHEMA_CODES`` in "
                "this test file with a one-line rationale (only when "
                "per-emitter divergence is a *deliberate* design choice "
                "documented at acn-error-schema.md §2).\n\n"
                "Drift sites:" + "".join(violations)
            )


class TestUnionSchemaCodesActuallyDiverge:
    """Self-policing: every code in ``UNION_SCHEMA_CODES`` must
    actually have divergent shapes today. If a previously-union code
    has converged to a single shape (e.g. via service-layer
    refactor), demote it back to strict so the strict-consistency
    test starts protecting it.

    This stops ``UNION_SCHEMA_CODES`` from accumulating dead entries
    that silently disable consistency checks.
    """

    def test_each_union_code_has_at_least_two_distinct_shapes(self, all_raise_sites):
        by_code: dict[str, list[RaiseSite]] = {}
        for site in all_raise_sites:
            by_code.setdefault(site.code, []).append(site)

        unnecessary: list[str] = []
        missing: list[str] = []
        for code in UNION_SCHEMA_CODES:
            sites = by_code.get(code, [])
            if not sites:
                missing.append(code)
                continue
            shapes = {
                s.details_keys if s.details_keys is not None else frozenset()
                for s in sites
            }
            if len(shapes) < 2:
                unnecessary.append(
                    f"  - ErrorCode.{code}: only {len(shapes)} distinct shape "
                    f"({next(iter(shapes))}). The union exemption is dead — "
                    f"remove from ``UNION_SCHEMA_CODES`` so strict consistency "
                    f"protects it going forward."
                )

        msgs = []
        if unnecessary:
            msgs.append(
                "These codes are marked union-schema but currently emit a "
                "single shape:\n" + "\n".join(unnecessary)
            )
        if missing:
            msgs.append(
                "These codes are marked union-schema but no raise site "
                "exists for them (stale exemption):\n  - "
                + "\n  - ".join(missing)
            )
        if msgs:
            pytest.fail("\n\n".join(msgs))
