"""HTTP routes for ACN blob objects."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.infrastructure.blob_store import FilesystemBlobStore
from acn.routes.blobs import get_blob_service, init_blob_service
from acn.routes.dependencies import get_agent_service, limiter
from acn.services.blob_service import BlobService
from tests.services.test_blob_service import FakeRedis, _settings


@pytest.fixture
def stub_agent_service():
    svc = AsyncMock()
    agent = SimpleNamespace(agent_id="agent-a", name="A", wallet_address=None)
    svc.get_agent_by_api_key = AsyncMock(return_value=agent)
    return svc


@pytest.fixture
def blob_env(tmp_path, stub_agent_service):
    limiter.enabled = False
    settings = _settings(tmp_path)
    service = BlobService(
        FakeRedis(),  # type: ignore[arg-type]
        FilesystemBlobStore(tmp_path),
        settings,  # type: ignore[arg-type]
        wallet=None,
    )
    init_blob_service(service)
    app.dependency_overrides[get_agent_service] = lambda: stub_agent_service
    app.dependency_overrides[get_blob_service] = lambda: service
    client = TestClient(app)
    yield client, service
    app.dependency_overrides.clear()
    limiter.enabled = True


def test_upload_usage_and_signed_download(blob_env) -> None:
    client, _service = blob_env
    res = client.post(
        "/api/v1/blobs",
        files={"file": ("hi.txt", b"hello", "text/plain")},
        headers={"Authorization": "Bearer acn_TEST"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["name"] == "hi.txt"
    assert body["size"] == 5
    uri = body["uri"]
    parsed = urlparse(uri)
    downloaded = client.get(f"{parsed.path}?{parsed.query}")
    assert downloaded.status_code == 200
    assert downloaded.content == b"hello"
    assert downloaded.headers["x-acn-blob-sha256"]

    usage = client.get(
        "/api/v1/blobs/usage",
        headers={"Authorization": "Bearer acn_TEST"},
    )
    assert usage.status_code == 200
    assert usage.json()["mailbox_bytes"] == 5
    assert usage.json()["used_bytes"] == 5


def test_download_without_sig_is_not_found(blob_env) -> None:
    client, service = blob_env
    res = client.post(
        "/api/v1/blobs",
        files={"file": ("hi.txt", b"hello", "text/plain")},
        headers={"Authorization": "Bearer acn_TEST"},
    )
    blob_id = res.json()["id"]
    leaked = client.get(f"/api/v1/blobs/{blob_id}")
    assert leaked.status_code == 404
    assert leaked.json()["error_code"] == "blob_not_found"


def test_owner_download_with_api_key(blob_env) -> None:
    client, _service = blob_env
    res = client.post(
        "/api/v1/blobs",
        files={"file": ("hi.txt", b"hello", "text/plain")},
        headers={"Authorization": "Bearer acn_TEST"},
    )
    blob_id = res.json()["id"]
    downloaded = client.get(
        f"/api/v1/blobs/{blob_id}",
        headers={"Authorization": "Bearer acn_TEST"},
    )
    assert downloaded.status_code == 200
    assert downloaded.content == b"hello"


def test_short_sig_is_not_found(blob_env) -> None:
    client, _service = blob_env
    res = client.post(
        "/api/v1/blobs",
        files={"file": ("hi.txt", b"hello", "text/plain")},
        headers={"Authorization": "Bearer acn_TEST"},
    )
    blob_id = res.json()["id"]
    leaked = client.get(f"/api/v1/blobs/{blob_id}?sig=ab")
    assert leaked.status_code == 404
    assert leaked.json()["error_code"] == "blob_not_found"


def test_empty_upload_rejected(blob_env) -> None:
    client, _service = blob_env
    res = client.post(
        "/api/v1/blobs",
        files={"file": ("empty.bin", b"", "application/octet-stream")},
        headers={"Authorization": "Bearer acn_TEST"},
    )
    assert res.status_code == 400
    assert res.json()["error_code"] == "invalid_request"


def test_upload_pathological_filename_is_basename(blob_env) -> None:
    client, _service = blob_env
    res = client.post(
        "/api/v1/blobs",
        files={"file": ("../../tmp/pwned.txt", b"hello", "text/plain")},
        headers={"Authorization": "Bearer acn_TEST"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["name"] == "pwned.txt"
    uri = res.json()["uri"]
    parsed = urlparse(uri)
    downloaded = client.get(f"{parsed.path}?{parsed.query}")
    assert downloaded.status_code == 200
    assert 'filename="pwned.txt"' in downloaded.headers["content-disposition"]
    assert ".." not in downloaded.headers["content-disposition"]


def test_download_strips_injected_mime_from_stored_meta(blob_env) -> None:
    client, service = blob_env
    res = client.post(
        "/api/v1/blobs",
        files={"file": ("hi.txt", b"hello", "text/plain")},
        headers={"Authorization": "Bearer acn_TEST"},
    )
    assert res.status_code == 200, res.text
    blob_id = res.json()["id"]
    key = f"acn:blob:{blob_id}"
    meta = json.loads(service.redis.kv[key])
    meta["mime_type"] = "text/plain\r\nLocation: https://evil.example"
    service.redis.kv[key] = json.dumps(meta)
    uri = res.json()["uri"]
    parsed = urlparse(uri)
    downloaded = client.get(f"{parsed.path}?{parsed.query}")
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("application/octet-stream")
    assert "Location" not in downloaded.headers
