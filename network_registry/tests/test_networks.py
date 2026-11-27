"""The directory lists the two hosted networks and does not join the ACN server."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from network_registry.app import create_app
from network_registry.catalog import Network, get_network, listed_networks
from network_registry.page import render_acnlist
from network_registry.store import Store

REPO = Path(__file__).resolve().parents[2]
KEY = "test-registry-key"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("REGISTRY_KEY", KEY)
    application = create_app(Store(tmp_path / "registry.db"))
    with TestClient(application) as test_client:
        yield test_client


def _register(**overrides: object) -> dict[str, object]:
    body = {
        "name": "Example",
        "network_id": "example-net",
        "framework": "acn",
        "gateway_url": "https://example.invalid",
        "listed": True,
    }
    body.update(overrides)
    return body


def test_acnlist_shows_the_listed_networks(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "acnlist" in body
    assert "ACN Global" in body
    assert "acn-global" in body
    assert "https://api.acnlabs.dev" in body
    assert "ACN CN" in body
    assert "acn-cn" in body
    assert "https://acn.acnlabs.cn" in body


def test_acnlist_omits_unlisted_and_escapes_names() -> None:
    hidden = Network(
        name="Private",
        network_id="private-net",
        framework="acn",
        gateway_url="https://example.invalid",
        listed=False,
    )
    messy = Network(
        name='<script>alert("x")</script>',
        network_id="messy",
        framework="acn",
        gateway_url="https://example.invalid/a?b=1&c=2",
        listed=True,
    )
    html = render_acnlist(listed_networks((messy, hidden)))
    assert "private-net" not in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "https://example.invalid/a?b=1&amp;c=2" in html


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_public_list_is_global_and_cn(client: TestClient) -> None:
    response = client.get("/networks")
    assert response.status_code == 200
    assert response.json() == {
        "networks": [
            {
                "name": "ACN Global",
                "network_id": "acn-global",
                "framework": "acn",
                "gateway_url": "https://api.acnlabs.dev",
                "listed": True,
            },
            {
                "name": "ACN CN",
                "network_id": "acn-cn",
                "framework": "acn",
                "gateway_url": "https://acn.acnlabs.cn",
                "listed": True,
            },
        ]
    }


def test_known_id_returns_its_gateway(client: TestClient) -> None:
    response = client.get("/networks/acn-cn")
    assert response.status_code == 200
    body = response.json()
    assert body["network_id"] == "acn-cn"
    assert body["gateway_url"] == "https://acn.acnlabs.cn"


def test_unknown_id_is_not_found(client: TestClient) -> None:
    response = client.get("/networks/does-not-exist")
    assert response.status_code == 404


def test_unlisted_record_stays_off_the_public_list() -> None:
    hidden = Network(
        name="Private",
        network_id="private-net",
        framework="acn",
        gateway_url="https://example.invalid",
        listed=False,
    )
    records = listed_networks() + (hidden,)
    assert hidden not in listed_networks(records)
    assert get_network("private-net", records) == hidden


def test_register_adds_a_listed_network(client: TestClient) -> None:
    response = client.post(
        "/networks",
        headers={"Authorization": f"Bearer {KEY}"},
        json=_register(),
    )
    assert response.status_code == 201
    assert response.json()["network_id"] == "example-net"
    listed = client.get("/networks").json()["networks"]
    assert listed[-1]["gateway_url"] == "https://example.invalid"
    assert "example-net" in client.get("/").text


def test_register_keeps_an_unlisted_network_off_the_page(client: TestClient) -> None:
    response = client.post(
        "/networks",
        headers={"Authorization": f"Bearer {KEY}"},
        json=_register(network_id="quiet-net", listed=False),
    )
    assert response.status_code == 201
    assert "quiet-net" not in client.get("/networks").text
    assert "quiet-net" not in client.get("/").text
    found = client.get("/networks/quiet-net")
    assert found.status_code == 200
    assert found.json()["listed"] is False


def test_register_rejects_a_duplicate_id(client: TestClient) -> None:
    response = client.post(
        "/networks",
        headers={"Authorization": f"Bearer {KEY}"},
        json=_register(network_id="acn-global", name="Other"),
    )
    assert response.status_code == 409
    assert client.get("/networks/acn-global").json()["name"] == "ACN Global"


def test_register_rejects_a_bad_key(client: TestClient) -> None:
    missing = client.post("/networks", json=_register())
    wrong = client.post(
        "/networks",
        headers={"Authorization": "Bearer no"},
        json=_register(),
    )
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert "example-net" not in client.get("/networks").text


def test_register_rejects_a_bad_record(client: TestClient) -> None:
    response = client.post(
        "/networks",
        headers={"Authorization": f"Bearer {KEY}"},
        json=_register(network_id="public", gateway_url="http://example.invalid"),
    )
    assert response.status_code == 422


def test_directory_is_not_part_of_the_acn_server() -> None:
    api = (REPO / "acn" / "api.py").read_text()
    dockerfile = (REPO / "Dockerfile").read_text()
    compose = (REPO / "docker-compose.yml").read_text()
    assert "network_registry" not in api
    assert "acn.api:app" in dockerfile
    assert "network_registry" not in dockerfile
    assert "network_registry" not in compose
