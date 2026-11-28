"""HTTP process for the network directory.

This process is not an ACN network and it is not mounted on ``acn.api:app``.
It only introduces a caller to a network's gateway. Registering an
already-running network writes a row in this process's own database.

From the ACN repo root:

    uv run --project network_registry uvicorn network_registry.app:app --host 127.0.0.1 --port 8010
"""

import os
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, field_validator

from network_registry.catalog import NETWORKS, Network
from network_registry.page import render_acnlist, render_start_network
from network_registry.store import Collision, Store

_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
_RESERVED_IDS = frozenset({"public", "system"})


class Registration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    network_id: str
    framework: str
    gateway_url: str
    listed: bool

    @field_validator("name")
    @classmethod
    def name_ok(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned or len(cleaned) > 80:
            raise ValueError("invalid name")
        return cleaned

    @field_validator("network_id")
    @classmethod
    def id_ok(cls, value: str) -> str:
        if value in _RESERVED_IDS or _ID.fullmatch(value) is None:
            raise ValueError("invalid network id")
        return value

    @field_validator("framework")
    @classmethod
    def framework_ok(cls, value: str) -> str:
        if _ID.fullmatch(value) is None or len(value) > 32:
            raise ValueError("invalid framework")
        return value

    @field_validator("gateway_url")
    @classmethod
    def gateway_ok(cls, value: str) -> str:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username
            or parsed.password
            or host in {"localhost", "127.0.0.1"}
        ):
            raise ValueError("gateway must be an https URL")
        return value


def create_app(store: Store | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        chosen = store if store is not None else Store(os.environ.get("REGISTRY_DB", "registry.db"))
        chosen.seed(NETWORKS)
        app.state.store = chosen
        app.state.registry_key = os.environ.get("REGISTRY_KEY", "")
        yield

    app = FastAPI(
        title="ACN Network Registry",
        description="Introduces a caller to a network gateway. Does not carry the call.",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/", response_class=HTMLResponse)
    def acnlist(request: Request) -> HTMLResponse:
        """Public directory page. Rows are the listed networks."""
        return HTMLResponse(render_acnlist(request.app.state.store.listed()))

    @app.get("/start", response_class=HTMLResponse)
    def start_network() -> HTMLResponse:
        """Steps for starting another native ACN. Does not register it."""
        return HTMLResponse(render_start_network())

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/networks")
    def list_networks(request: Request) -> dict[str, list[dict[str, str | bool]]]:
        """Listed networks only."""
        return {"networks": [_record(network) for network in request.app.state.store.listed()]}

    @app.get("/networks/{network_id}")
    def read_network(network_id: str, request: Request) -> dict[str, str | bool]:
        """Introduce a known network id, including one that is not listed."""
        network = request.app.state.store.get(network_id)
        if network is None:
            raise HTTPException(status_code=404, detail="network not found")
        return _record(network)

    @app.post("/networks", status_code=201)
    def register_network(body: Registration, request: Request) -> dict[str, str | bool]:
        """Record a network that is already running. A duplicate id is rejected."""
        _require_key(request)
        network = Network(
            name=body.name,
            network_id=body.network_id,
            framework=body.framework,
            gateway_url=body.gateway_url,
            listed=body.listed,
        )
        try:
            request.app.state.store.add(network)
        except Collision:
            raise HTTPException(status_code=409, detail="network id already registered") from None
        return _record(network)

    return app


def _require_key(request: Request) -> None:
    expected = request.app.state.registry_key
    if not expected:
        raise HTTPException(status_code=503, detail="registry key is not configured")
    header = request.headers.get("authorization", "")
    token = header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""
    if not token or len(token) != len(expected) or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="registry key rejected")


def _record(network: Network) -> dict[str, str | bool]:
    return {
        "name": network.name,
        "network_id": network.network_id,
        "framework": network.framework,
        "gateway_url": network.gateway_url,
        "listed": network.listed,
    }


app = create_app()
