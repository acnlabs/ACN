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
    assert "Network ID" in body


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
                "seq": 1,
            },
            {
                "name": "ACN CN",
                "network_id": "acn-cn",
                "framework": "acn",
                "gateway_url": "https://acn.acnlabs.cn",
                "listed": True,
                "seq": 2,
            },
        ]
    }


def test_seq_is_assigned_in_registration_order(client: TestClient) -> None:
    response = client.post(
        "/networks",
        headers={"Authorization": f"Bearer {KEY}"},
        json=_register(),
    )
    assert response.status_code == 201
    assert response.json()["seq"] == 3
    assert client.get("/networks/example-net").json()["seq"] == 3


def test_existing_database_gets_seq_backfilled(tmp_path, monkeypatch) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE networks (network_id TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "framework TEXT NOT NULL, gateway_url TEXT NOT NULL, listed INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO networks VALUES ('acn-global', 'ACN Global', 'acn', 'https://api.acnlabs.dev', 1)"
    )
    conn.execute(
        "INSERT INTO networks VALUES ('acn-cn', 'ACN CN', 'acn', 'https://acn.acnlabs.cn', 1)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("REGISTRY_KEY", KEY)
    with TestClient(create_app(Store(path))) as test_client:
        networks = test_client.get("/networks").json()["networks"]
    assert [row["seq"] for row in networks] == [1, 2]


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


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {KEY}"}


def test_patch_changes_fields_but_never_id_or_seq(client: TestClient) -> None:
    client.post("/networks", headers=_auth(), json=_register())
    response = client.patch(
        "/networks/example-net",
        headers=_auth(),
        json={"name": "Renamed", "listed": False, "gateway_url": "https://moved.example"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Renamed"
    assert body["gateway_url"] == "https://moved.example"
    assert body["listed"] is False
    assert body["network_id"] == "example-net"
    assert body["seq"] == 3
    assert "example-net" not in client.get("/networks").text
    assert client.get("/networks/example-net").json()["seq"] == 3


def test_patch_requires_the_key(client: TestClient) -> None:
    client.post("/networks", headers=_auth(), json=_register())
    missing = client.patch("/networks/example-net", json={"name": "Nope"})
    wrong = client.patch(
        "/networks/example-net",
        headers={"Authorization": "Bearer no"},
        json={"name": "Nope"},
    )
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert client.get("/networks/example-net").json()["name"] == "Example"


def test_patch_unknown_id_is_not_found(client: TestClient) -> None:
    response = client.patch(
        "/networks/does-not-exist", headers=_auth(), json={"name": "Nope"}
    )
    assert response.status_code == 404


def test_patch_rejects_empty_and_bad_changes(client: TestClient) -> None:
    client.post("/networks", headers=_auth(), json=_register())
    empty = client.patch("/networks/example-net", headers=_auth(), json={})
    bad_gateway = client.patch(
        "/networks/example-net", headers=_auth(), json={"gateway_url": "http://x.invalid"}
    )
    extra = client.patch(
        "/networks/example-net", headers=_auth(), json={"seq": 9}
    )
    assert empty.status_code == 422
    assert bad_gateway.status_code == 422
    assert extra.status_code == 422
    assert client.get("/networks/example-net").json()["seq"] == 3


def test_delete_removes_a_network_without_renumbering(client: TestClient) -> None:
    client.post("/networks", headers=_auth(), json=_register())
    removed = client.delete("/networks/example-net", headers=_auth())
    assert removed.status_code == 204
    assert client.get("/networks/example-net").status_code == 404
    remaining = client.get("/networks").json()["networks"]
    assert [row["seq"] for row in remaining] == [1, 2]
    again = client.delete("/networks/example-net", headers=_auth())
    assert again.status_code == 404


def test_delete_requires_the_key(client: TestClient) -> None:
    missing = client.delete("/networks/acn-cn")
    wrong = client.delete("/networks/acn-cn", headers={"Authorization": "Bearer no"})
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert client.get("/networks/acn-cn").status_code == 200


def test_seeded_network_returns_after_delete_and_restart(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("REGISTRY_KEY", KEY)
    path = tmp_path / "registry.db"
    with TestClient(create_app(Store(path))) as first:
        assert first.delete("/networks/acn-cn", headers=_auth()).status_code == 204
        assert "acn-cn" not in first.get("/networks").text
    with TestClient(create_app(Store(path))) as second:
        networks = second.get("/networks").json()["networks"]
    assert [row["network_id"] for row in networks] == ["acn-global", "acn-cn"]
    assert [row["seq"] for row in networks] == [1, 2]


def test_directory_is_not_part_of_the_acn_server() -> None:
    api = (REPO / "acn" / "api.py").read_text()
    dockerfile = (REPO / "Dockerfile").read_text()
    compose = (REPO / "docker-compose.yml").read_text()
    assert "network_registry" not in api
    assert "acn.api:app" in dockerfile
    assert "network_registry" not in dockerfile
    assert "network_registry" not in compose


def test_start_steps_are_the_same_for_people_and_agents(client: TestClient) -> None:
    command = (
        "docker compose --env-file .env.network -p acn-one "
        "-f docker-compose.network.yml up -d --build"
    )
    page = client.get("/start")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert command in page.text
    assert "REGISTRY_KEY" not in page.text
    assert "<form" not in page.text
    home = client.get("/")
    assert 'href="/start"' in home.text
    skill = (REPO / "skills" / "acn" / "SKILL.md").read_text()
    assert command in skill
    assert "REGISTRY_KEY" not in skill
    assert 'version: "1.0.16"' in skill


def test_native_network_compose_is_its_own_acn() -> None:
    compose = (REPO / "docker-compose.network.yml").read_text()
    example = (REPO / "env.network.example").read_text()
    ignore = (REPO / ".gitignore").read_text()
    assert "network_registry" not in compose
    assert "container_name:" not in compose
    assert "build: ." in compose
    assert "DATABASE_URL: \"\"" in compose
    assert "redis://redis:6379/0" in compose
    assert "${ACN_NETWORK_PORT:-9100}:8000" in compose
    assert "api.acnlabs.dev" not in example
    assert "acn.acnlabs.cn" not in example
    assert "REGISTRY_KEY" not in example
    assert ".env.network" in ignore
