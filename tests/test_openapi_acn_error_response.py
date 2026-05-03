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
        # manifest (#8) shares the ``/api/v1/communication`` prefix
        # with the ✅-migrated ``communication`` router but registers
        # its own ``APIRouter``. Sample the content-fetch endpoint
        # because its emitted code (``MANIFEST_CONTENT_NOT_FOUND``)
        # has the more security-sensitive details shape — naming the
        # field ``owner_id`` rather than ``agent_id`` is the contract
        # SDK type-gen reads, and a router-config regression here
        # would silently drop that distinction.
        (
            "/api/v1/communication/content/{mid}",
            "get",
            "manifest (#8) — GET content",
        ),
        # analytics (#9) only has 4xx raise sites on a *single*
        # endpoint — ``GET /api/v1/analytics/activities`` with the
        # ``agent_id`` / ``agent_ids`` filter. The other six
        # analytics endpoints have no file-local 4xx sites
        # (``InternalTokenDep`` covers their auth). We sample the
        # filter-bearing endpoint because that's where the router
        # config materially impacts SDK type-gen — a regression on
        # the router's ``responses=`` argument would drop the
        # ``ACNErrorResponse`` ref from precisely the endpoint
        # SDK clients hit when filtering activity feeds.
        (
            "/api/v1/analytics/activities",
            "get",
            "analytics (#9) — GET activities",
        ),
        # onchain (#7) carries the most diverse error vocabulary
        # of any non-pilot router (6 new ErrorCodes +
        # 2 reused). We sample ``POST /agents/{id}/bind``
        # because it is the only endpoint that can raise *all*
        # six new ERC-8004 codes (chain mismatch, token already
        # bound, registration mismatch) and one of the four
        # AGENT_NOT_FOUND sites — a router-config regression on
        # the bind endpoint catches the most surface area in a
        # single sample. The other onchain endpoints (GET
        # identity / reputation / validation) inherit the same
        # router-level ``responses=`` block so positive coverage
        # transfers to them by FastAPI's uniform merge.
        (
            "/api/v1/onchain/agents/{agent_id}/bind",
            "post",
            "onchain (#7) — POST bind",
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

    Empty as of sprint #7
    ---------------------
    All HTTP-mounting routers are migrated. The remaining sprint
    (#11 ``websocket``) does NOT register an HTTP router — it owns
    a WebSocket endpoint whose error contract is bounded by RFC
    6455 close codes, not HTTP responses. The negative-coverage
    contract therefore has no rows to assert today.

    The class itself stays in the file as a structural anchor: if
    a future contributor adds a new non-migrated HTTP router (e.g.
    a brand new ``/api/v1/foo/*`` namespace that raises raw
    ``HTTPException``), they should add an entry here at the same
    time as the router lands so drift detection re-engages — the
    same lifecycle every previous sprint exercised. The
    ``test_class_remains_a_structural_anchor`` test below is a
    no-op intended only to keep the class non-empty so it shows
    up in pytest collection output as a documented contract.
    """

    NON_MIGRATED_ENDPOINTS: list[tuple[str, str, str]] = [
        # Empty as of sprint #7 — see class docstring. Future
        # non-migrated HTTP routers go here in the (path, method,
        # module name for failure messages) shape.
    ]

    def test_class_remains_a_structural_anchor(self):
        """Documentation-only assertion.

        Keeps this class visible in pytest output (and in code
        review diffs that touch the migration matrix) even when
        ``NON_MIGRATED_ENDPOINTS`` is empty, so the contract
        described in the class docstring is discoverable. If a
        future ``parametrize`` over the empty list is collected,
        pytest emits a warning; pinning a single anchor test
        avoids that noise.
        """
        # Intentional: at sprint #7 the list MUST be empty. If a
        # future sprint adds rows back, this assertion fails by
        # design — the contributor reads the class docstring,
        # adds their entry to ``NON_MIGRATED_ENDPOINTS``, and
        # parametrizes the test below over the new list.
        assert self.NON_MIGRATED_ENDPOINTS == [], (
            "NON_MIGRATED_ENDPOINTS is no longer empty — port the "
            "test_non_migrated_router_has_no_default_4xx_block test "
            "back from git history (commits prior to sprint #7) and "
            "re-parametrize over the new list."
        )
