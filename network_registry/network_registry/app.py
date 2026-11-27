"""HTTP process for the network directory.

This process is not an ACN network and it is not mounted on ``acn.api:app``.
It only introduces a caller to a network's gateway.

From the ACN repo root:

    uv run --project network_registry uvicorn network_registry.app:app --host 127.0.0.1 --port 8010
"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from network_registry.catalog import Network, get_network, listed_networks
from network_registry.page import render_acnlist

app = FastAPI(
    title="ACN Network Registry",
    description="Introduces a caller to a network gateway. Does not carry the call.",
    version="0.1.0",
)


def _record(network: Network) -> dict[str, str | bool]:
    return {
        "name": network.name,
        "network_id": network.network_id,
        "framework": network.framework,
        "gateway_url": network.gateway_url,
        "listed": network.listed,
    }


@app.get("/", response_class=HTMLResponse)
def acnlist() -> HTMLResponse:
    """Public directory page. Rows are the listed networks, not a hardcoded pair."""
    return HTMLResponse(render_acnlist(listed_networks()))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/networks")
def list_networks() -> dict[str, list[dict[str, str | bool]]]:
    """Listed networks only. acnlist reads this list."""
    return {"networks": [_record(network) for network in listed_networks()]}


@app.get("/networks/{network_id}")
def read_network(network_id: str) -> dict[str, str | bool]:
    """Introduce a known network id, including one that is not listed."""
    network = get_network(network_id)
    if network is None:
        raise HTTPException(status_code=404, detail="network not found")
    return _record(network)
