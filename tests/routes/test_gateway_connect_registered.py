"""Contract: subnet gateway websocket is reachable on the root app.

Regression guard for deployments where ``SubnetManager.handle_connection``
was wired in lifespan but no route advertised it — production returned 404
for ``GET /gateway/connect/...``.
"""


def test_gateway_connect_websocket_route_exists_on_main_app():
    from acn.api import app

    paths = tuple(
        getattr(r, "path", "")
        for r in app.routes
        if getattr(r, "path", "").startswith("/gateway/connect")
    )
    assert paths, "expected at least one /gateway/connect websocket route"

