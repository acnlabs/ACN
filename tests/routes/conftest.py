"""Shared fixtures and helpers for ``tests/routes/*``.

Hoisted from the per-file copies that sprint rows #1, #2a, and #3 of
the Phase 2 review v2 P1 #11 error-schema migration accumulated. See
the resolved P3 ticket "Hoist shared route-test fixtures" in
``docs/BACKLOG.md`` for the trigger / migration story.

What lives here
---------------

``_reset_state`` (autouse)
    Resets the three pieces of process-global state every route test
    file in this directory cared about: the SlowAPI rate limiter
    (which would otherwise re-introduce a Redis dependency), the
    in-memory API-key resolution cache in
    ``acn.routes.dependencies`` (which would otherwise short-circuit
    per-test mocks of ``agent_service.get_agent_by_api_key``), and
    ``app.dependency_overrides`` (which would otherwise leak across
    tests).

``_FLAT_SCHEMA_FIELDS``
    The canonical set of top-level keys an ``ACNHTTPError`` response
    body MUST contain. Used by ``_assert_flat_shape`` and the
    ``error-schema`` test files.

``_assert_flat_shape(body)``
    Raises ``AssertionError`` with a diagnostic if a response body
    departs from the canonical flat shape. The error message points
    at the most common regression root cause (a stray
    ``raise HTTPException(...)``).

Why ``autouse`` *and* fixture override
--------------------------------------

The ``_reset_state`` fixture is ``autouse=True`` so any new
route-test file picks it up by default. Six pre-existing route test
files (``test_allowlist_routes.py``, ``test_agent_endpoint_disclosure.py``,
``test_agent_policy_patch.py``, ``test_manifest_routes.py``,
``test_agent_social_card_url_patch.py``, ``test_agent_card_url_sanitize.py``)
already define their own ``_reset_state`` autouse fixtures. Pytest's
fixture override rules give the *closest-to-test* fixture priority,
so those six files continue to use their own copies — the conftest
fixture defined here is silently overridden in those scopes.

That override is **intentional**, not a hazard:

* Five of the six file-local copies are byte-identical to this one;
  the override is a no-op.
* The sixth (``test_agent_card_url_sanitize.py``) deliberately omits
  the API-key-cache clear because that test doesn't authenticate; the
  override preserves that minimal-scope behaviour without forcing the
  conftest version on it.

Future schema migration sprints (rows #4–#11) get the conftest
version "for free" — they only need to define their stub services,
not re-derive the reset boilerplate.

Trigger condition for renaming
------------------------------

If a future route test deliberately wants the file-local version
*plus* the conftest version both to run (e.g. file-local resets a
secondary cache and wants the standard reset on top), rename the
file-local fixture to a different name so both autouse fixtures run.
The current state — six files override, three files inherit — is
the right balance for the migration sprint.
"""

from __future__ import annotations

import pytest

from acn.api import app
from acn.routes.dependencies import _api_key_cache, limiter


@pytest.fixture(autouse=True)
def _reset_state():
    limiter.enabled = False
    _api_key_cache.clear()
    yield
    limiter.enabled = True
    _api_key_cache.clear()
    app.dependency_overrides.clear()


_FLAT_SCHEMA_FIELDS = {"error_code", "message", "details", "request_id"}


def _assert_flat_shape(body: dict) -> None:
    """Assert that ``body`` matches the canonical ACN flat-error shape.

    The diagnostic on failure points at the most common regression
    root cause: a ``raise HTTPException(...)`` sneaked back into a
    migrated handler, so the response carries the legacy ``detail``
    field instead of the flat ``error_code`` / ``message`` /
    ``details`` / ``request_id`` quartet.
    """
    assert _FLAT_SCHEMA_FIELDS <= body.keys(), (
        f"missing canonical fields; got {sorted(body.keys())}"
    )
    assert "detail" not in body, (
        "legacy `detail` field present — migration regression: a "
        "raise HTTPException(...) likely sneaked back in"
    )
    assert isinstance(body["details"], dict), (
        "`details` must be a JSON object even when empty"
    )
