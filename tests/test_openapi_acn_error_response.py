"""OpenAPI schema visibility for ACN flat error response.

P3 ticket follow-up: pin that ``/openapi.json`` advertises
``ACNErrorResponse`` as the 4xx response model for every endpoint
served by a router that opts in via
``responses=ACN_DEFAULT_RESPONSES`` (i.e. every router that raises
``ACNHTTPError``). Without these tests, a refactor that drops the
``responses=`` argument from one of the routers would silently
regress SDK type-gen output for that module — generated client
4xx response types would fall back to ``HTTPValidationError`` /
generic ``dict``.

Coverage choices
----------------
* We assert *router-level* coverage by sampling at least one
  endpoint per migrated module and checking that all six default
  status codes (400/401/403/404/409/429) reference
  ``ACNErrorResponse`` in the generated spec. Listing every
  endpoint × status code is unnecessary — FastAPI's router-level
  ``responses=`` mechanism is uniform; if one endpoint per router
  has the spec, all do.
* We also pin the ``components.schemas.ACNErrorResponse`` block
  itself: name + the four-field flat shape contract
  (``error_code`` / ``message`` / ``details`` / ``request_id``).
  This is the contract SDK type-gen consumers actually read.
* 422 is intentionally NOT asserted: FastAPI auto-emits 422 with
  its own ``HTTPValidationError`` schema; alignment with the ACN
  flat shape is tracked as a separate P3 ticket. Asserting on it
  here would prematurely pin a schema we explicitly chose not to
  align yet.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from acn.api import app


@pytest.fixture(scope="module")
def openapi_spec() -> dict:
    """Fetch the FastAPI-generated OpenAPI spec once per module.

    Uses ``TestClient`` rather than calling ``app.openapi()`` directly
    so the spec is exactly what an SDK type-gen consumer would see
    over HTTP.
    """
    with TestClient(app) as client:
        r = client.get("/openapi.json")
        assert r.status_code == 200, f"openapi.json fetch failed: {r.status_code}"
        return r.json()


class TestACNErrorResponseSchemaPresence:
    """The flat error response model itself is in components.schemas."""

    def test_acn_error_response_schema_is_advertised(self, openapi_spec):
        schemas = openapi_spec.get("components", {}).get("schemas", {})
        assert "ACNErrorResponse" in schemas, (
            "ACNErrorResponse must be advertised in components.schemas. "
            "If this fails, no router opts into ``responses=ACN_DEFAULT_RESPONSES``, "
            "or the router-level mechanism stopped working."
        )

    def test_acn_error_response_carries_four_canonical_fields(self, openapi_spec):
        """The flat schema's contract: exactly these four fields,
        all required (well, ``details`` defaults to ``{}`` server-side
        but is always present in the body)."""
        schema = openapi_spec["components"]["schemas"]["ACNErrorResponse"]
        properties = schema.get("properties", {})
        assert set(properties.keys()) == {
            "error_code",
            "message",
            "details",
            "request_id",
        }, (
            f"ACNErrorResponse fields drifted: {sorted(properties.keys())}. "
            f"The four-field flat shape is the ACN public schema contract — "
            f"changes here are SDK-breaking and must be coordinated through "
            f"the SDK release-notes channel."
        )

    def test_acn_error_response_required_fields(self, openapi_spec):
        """Pin which fields are ``required`` in the OpenAPI sense.
        ``details`` has a server-side default (``{}``) but is still
        emitted unconditionally — pydantic marks it required when
        ``default_factory`` is set without ``optional=True`` semantics."""
        schema = openapi_spec["components"]["schemas"]["ACNErrorResponse"]
        required = set(schema.get("required", []))
        assert {"error_code", "message", "request_id"} <= required, (
            f"error_code / message / request_id must be required in the "
            f"OpenAPI schema (SDK type-gen relies on these being non-Optional). "
            f"Got required={sorted(required)}."
        )


class TestRouterLevelResponsesCoverage:
    """Every migrated router opts into the default ``responses=``
    block, so all of its endpoints advertise the six default 4xx
    status codes mapping to ``ACNErrorResponse``.

    Why one representative endpoint per router (not all of them):
    FastAPI's router-level ``responses=`` arg is uniform — it merges
    into every endpoint's spec at registration time. If the
    representative endpoint has the spec, all endpoints in that
    router have it. Sampling keeps the test fast and the failure
    output focused; per-endpoint coverage would balloon to 50+
    near-identical assertions for zero added signal.
    """

    EXPECTED_STATUSES = {"400", "401", "403", "404", "409", "429"}

    REPRESENTATIVE_ENDPOINTS = [
        # (path, method, module name for failure messages)
        ("/api/v1/communication/send", "post", "communication (pilot)"),
        # allowlist + registry both share the ``/api/v1/agents`` prefix;
        # we sample one route from each so a regression on either router
        # is caught (the two ``APIRouter(...)`` calls are independent
        # despite the prefix overlap).
        ("/api/v1/agents/{agent_id}/allowlist", "get", "allowlist (#1) — GET listing"),
        ("/api/v1/agents/{agent_id}/allowlist/{target_id}", "post", "allowlist (#1) — POST add"),
        ("/api/v1/agents/register", "post", "registry (#2)"),
        ("/api/v1/subnets", "post", "subnets (#3)"),
        ("/api/v1/tasks", "post", "tasks (#4)"),
        ("/api/v1/payments/tasks", "post", "payments (#5)"),
        # follows (#6) shares the ``/api/v1/agents`` prefix with both
        # ``allowlist`` and ``registry``, but registers its own
        # ``APIRouter`` with its own ``responses=ACN_DEFAULT_RESPONSES``
        # — sample one follow-specific path so a regression on
        # follows.py's router config is caught even when allowlist /
        # registry remain healthy.
        (
            "/api/v1/agents/{agent_id}/follows/{target_id}",
            "post",
            "follows (#6) — POST follow",
        ),
    ]

    @pytest.mark.parametrize(
        "path,method,module",
        REPRESENTATIVE_ENDPOINTS,
        ids=[m for _, _, m in REPRESENTATIVE_ENDPOINTS],
    )
    def test_router_advertises_default_4xx_responses(
        self, openapi_spec, path: str, method: str, module: str
    ):
        paths = openapi_spec.get("paths", {})
        assert path in paths, (
            f"{module}: representative path {path!r} not found in spec — "
            f"either the route was renamed/removed or the prefix changed. "
            f"Available paths: {sorted(paths.keys())[:15]}..."
        )
        op = paths[path].get(method)
        assert op is not None, (
            f"{module}: {method.upper()} {path} not found — has the HTTP "
            f"method changed?"
        )
        responses = op.get("responses", {})
        missing = self.EXPECTED_STATUSES - set(responses.keys())
        assert not missing, (
            f"{module}: {method.upper()} {path} is missing default "
            f"status codes {sorted(missing)} from its OpenAPI responses. "
            f"This usually means the router lost its "
            f"``responses=ACN_DEFAULT_RESPONSES`` argument."
        )

    @pytest.mark.parametrize(
        "path,method,module",
        REPRESENTATIVE_ENDPOINTS,
        ids=[m for _, _, m in REPRESENTATIVE_ENDPOINTS],
    )
    def test_default_4xx_responses_reference_acn_error_response(
        self, openapi_spec, path: str, method: str, module: str
    ):
        """Each of the six default status codes references the
        ``ACNErrorResponse`` schema (not ``HTTPValidationError`` or
        an inline anonymous schema)."""
        op = openapi_spec["paths"][path][method]
        for status in sorted(self.EXPECTED_STATUSES):
            response = op["responses"][status]
            content = response.get("content", {})
            json_content = content.get("application/json", {})
            ref = json_content.get("schema", {}).get("$ref")
            assert ref == "#/components/schemas/ACNErrorResponse", (
                f"{module}: {method.upper()} {path} response {status} "
                f"should reference ACNErrorResponse, got ref={ref!r}. "
                f"This is the contract SDK type-gen reads — drift here "
                f"silently changes generated client types."
            )


class TestNonMigratedRoutersDoNotAdvertiseDefault:
    """Negative coverage — drift detection for the migration matrix.

    The positive tests above prove that *migrated* routers advertise
    the default 4xx block. This class proves the converse: routers
    that have *not* been migrated to ``ACNHTTPError`` do *not*
    advertise the default block. Without this guard, a contributor
    who slaps ``responses=ACN_DEFAULT_RESPONSES`` onto a router
    without flipping its raise sites to ``ACNHTTPError`` would
    silently make the OpenAPI spec over-promise: SDK type-gen would
    emit ``ACNErrorResponse`` typings while the runtime still emits
    the legacy ``{"detail": ...}`` shape — the worst kind of
    contract bug because it only surfaces in production at the
    SDK / client deserialisation layer.

    Sampled non-migrated routers (matrix as of this commit):
    - ``manifest`` (sprint #8, pending) — owns the
      ``/api/v1/communication/manifest/*`` and ``/content/*``
      endpoints. Notably this one shares the
      ``/api/v1/communication`` URL prefix with the ✅-migrated
      ``communication`` router (see "5 routers ≠ 5 URL prefixes"
      caveat in BACKLOG); the negative test pins the
      heterogeneity — a single shared URL prefix can host
      heterogeneously-migrated routers, and OpenAPI tracks the
      router (where ``responses=`` is declared), not the prefix.

    When sprint #8 / #9 / #10 / #11 land, move the corresponding
    entry from here up into ``REPRESENTATIVE_ENDPOINTS`` above and
    the drift-detection contract converts to a positive coverage
    contract atomically.
    """

    NON_MIGRATED_ENDPOINTS = [
        # (path, method, module name for failure messages)
        (
            "/api/v1/communication/content/{mid}",
            "get",
            "manifest (sprint #8 — NOT YET migrated)",
        ),
    ]

    @pytest.mark.parametrize(
        "path,method,module",
        NON_MIGRATED_ENDPOINTS,
        ids=[m for _, _, m in NON_MIGRATED_ENDPOINTS],
    )
    def test_non_migrated_router_has_no_default_4xx_block(
        self, openapi_spec, path: str, method: str, module: str
    ):
        op = openapi_spec["paths"][path][method]
        responses = op.get("responses", {})
        unexpected_4xx = (
            TestRouterLevelResponsesCoverage.EXPECTED_STATUSES
            & set(responses.keys())
        )
        assert not unexpected_4xx, (
            f"{module}: {method.upper()} {path} unexpectedly advertises "
            f"default 4xx codes {sorted(unexpected_4xx)}. Either:\n"
            f"  (a) this module just got migrated to ACNHTTPError + "
            f"``responses=ACN_DEFAULT_RESPONSES`` — congrats, move this "
            f"entry from NON_MIGRATED_ENDPOINTS up into "
            f"REPRESENTATIVE_ENDPOINTS in the class above; or\n"
            f"  (b) someone added ``responses=ACN_DEFAULT_RESPONSES`` to "
            f"the router without flipping the raise sites — that's a "
            f"contract bug (spec promises ACNErrorResponse, runtime "
            f"emits legacy ``{{detail}}``); revert the responses= until "
            f"the raise sites are migrated."
        )
