"""The directory lists the two hosted networks and does not join the ACN server."""

from pathlib import Path

from fastapi.testclient import TestClient
from network_registry.app import app
from network_registry.catalog import Network, get_network, listed_networks

client = TestClient(app)
REPO = Path(__file__).resolve().parents[2]


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_public_list_is_global_and_cn() -> None:
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


def test_known_id_returns_its_gateway() -> None:
    response = client.get("/networks/acn-cn")
    assert response.status_code == 200
    body = response.json()
    assert body["network_id"] == "acn-cn"
    assert body["gateway_url"] == "https://acn.acnlabs.cn"


def test_unknown_id_is_not_found() -> None:
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


def test_directory_is_not_part_of_the_acn_server() -> None:
    api = (REPO / "acn" / "api.py").read_text()
    dockerfile = (REPO / "Dockerfile").read_text()
    compose = (REPO / "docker-compose.yml").read_text()
    assert "network_registry" not in api
    assert "acn.api:app" in dockerfile
    assert "network_registry" not in dockerfile
    assert "network_registry" not in compose
