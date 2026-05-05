"""Phase 3 content_url — POST /communication/send and GET /manifest/{mid}/content.

Pins the route-layer contract for the self-hosted content path:

* Happy path: ``content_url`` + ``content_hash`` forwarded to ``send_message``
  as keyword args; ``message`` is still required.
* Schema validation: non-http(s) URL → 422 (Pydantic field validator);
  ``content_hash`` without ``content_url`` is silently dropped (no error).
* ``ContentUrlWrongModeError`` (router-side) → 400
  ``CONTENT_URL_REQUIRES_MANIFEST_MODE`` so the sender knows ACN did *not*
  skip payload storage.
* Content fetch — self-hosted branch: ``GET /manifest/{mid}/content`` returns
  ``{"self_hosted": true, "content_url": ..., "content_hash": ...}`` without a
  ``content`` key when the manifest entry carries ``content_url``.
* Content fetch — ACN-hosted branch (regression): existing behaviour unchanged.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.infrastructure.messaging.message_router import ContentUrlWrongModeError
from acn.routes.dependencies import (
    get_audit,
    get_message_service,
    get_metrics,
    limiter,
    verify_agent_api_key,
)
from acn.services.manifest_service import ManifestEntry

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_metrics():
    m = AsyncMock()
    m.inc_message_count = AsyncMock()
    m.inc_counter = AsyncMock()
    return m


@pytest.fixture
def stub_audit():
    a = AsyncMock()
    a.log_event = AsyncMock()
    return a


@pytest.fixture
def stub_message_service():
    """Default: returns a manifest-mode receipt with content_url echoed back."""
    svc = AsyncMock()
    svc.send_message = AsyncMock(
        return_value={
            "status": "sent",
            "delivery_mode": "manifest",
            "route_id": "rt_abc123",
            "mid": "a" * 32,
            "ts": 1_700_000_000_000,
            "content_url": "https://example.com/payload.json",
            "content_hash": "sha256:deadbeef",
        }
    )
    return svc


@pytest.fixture(autouse=True)
def _reset_overrides_and_limiter():
    limiter.enabled = False
    yield
    limiter.enabled = True
    app.dependency_overrides.clear()


def _wire_send(metrics, message_service, audit) -> None:
    app.dependency_overrides[get_metrics] = lambda: metrics
    app.dependency_overrides[get_message_service] = lambda: message_service
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[verify_agent_api_key] = lambda: {"agent_id": "agent-sender"}


def _send_body(**overrides) -> dict:
    body: dict = {
        "from_agent": "agent-sender",
        "target_agent": "agent-target",
        "message": {"text": "hello"},
        "priority": "normal",
        "content_url": "https://example.com/payload.json",
        "content_hash": "sha256:deadbeef",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# 1. Happy path — send with content_url
# ---------------------------------------------------------------------------


class TestSendWithContentUrl:
    def test_happy_path_forwards_url_and_hash_to_service(
        self, stub_metrics, stub_message_service, stub_audit
    ):
        _wire_send(stub_metrics, stub_message_service, stub_audit)

        with patch("acn.routes.communication.Message", return_value=MagicMock()):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/send",
                    json=_send_body(),
                )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["delivery_mode"] == "manifest"
        assert body["content_url"] == "https://example.com/payload.json"
        assert body["content_hash"] == "sha256:deadbeef"

        kwargs = stub_message_service.send_message.await_args.kwargs
        assert kwargs["content_url"] == "https://example.com/payload.json"
        assert kwargs["content_hash"] == "sha256:deadbeef"

    def test_url_only_no_hash_is_valid(
        self, stub_metrics, stub_message_service, stub_audit
    ):
        """content_url without content_hash is allowed."""
        stub_message_service.send_message.return_value = {
            "status": "sent",
            "delivery_mode": "manifest",
            "route_id": "rt_abc123",
            "mid": "b" * 32,
            "ts": 1_700_000_000_000,
            "content_url": "https://example.com/payload.json",
        }
        _wire_send(stub_metrics, stub_message_service, stub_audit)

        with patch("acn.routes.communication.Message", return_value=MagicMock()):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/send",
                    json=_send_body(content_hash=None),
                )

        assert r.status_code == 200, r.text
        kwargs = stub_message_service.send_message.await_args.kwargs
        assert kwargs["content_url"] == "https://example.com/payload.json"
        assert "content_hash" not in kwargs

    def test_hash_without_url_is_silently_dropped(
        self, stub_metrics, stub_audit
    ):
        """content_hash without content_url must not be forwarded to the service."""
        svc = AsyncMock()
        svc.send_message = AsyncMock(
            return_value={
                "status": "sent",
                "delivery_mode": "manifest",
                "route_id": "rt_abc123",
                "mid": "c" * 32,
                "ts": 1_700_000_000_000,
            }
        )
        _wire_send(stub_metrics, svc, stub_audit)

        with patch("acn.routes.communication.Message", return_value=MagicMock()):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/send",
                    json=_send_body(content_url=None, content_hash="sha256:deadbeef"),
                )

        assert r.status_code == 200, r.text
        kwargs = svc.send_message.await_args.kwargs
        assert "content_url" not in kwargs
        assert "content_hash" not in kwargs


# ---------------------------------------------------------------------------
# 2. Schema validation
# ---------------------------------------------------------------------------


class TestContentUrlSchemaValidation:
    @pytest.mark.parametrize(
        "bad_url",
        [
            "ftp://evil.com/payload",
            "not-a-url",
            "javascript:alert(1)",
            "//no-scheme.com/payload",
        ],
    )
    def test_non_http_url_returns_422(
        self, stub_metrics, stub_message_service, stub_audit, bad_url
    ):
        _wire_send(stub_metrics, stub_message_service, stub_audit)

        with patch("acn.routes.communication.Message", return_value=MagicMock()):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/send",
                    json=_send_body(content_url=bad_url),
                )

        assert r.status_code == 422, r.text
        stub_message_service.send_message.assert_not_awaited()

    def test_http_url_accepted(self, stub_metrics, stub_message_service, stub_audit):
        """Plain http:// (not just https://) must also be accepted."""
        stub_message_service.send_message.return_value = {
            "status": "sent",
            "delivery_mode": "manifest",
            "route_id": "rt_abc123",
            "mid": "d" * 32,
            "ts": 1_700_000_000_000,
        }
        _wire_send(stub_metrics, stub_message_service, stub_audit)

        with patch("acn.routes.communication.Message", return_value=MagicMock()):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/send",
                    json=_send_body(content_url="http://internal.example.com/p"),
                )

        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 3. Router error mapping — ContentUrlWrongModeError → 400
# ---------------------------------------------------------------------------


class TestContentUrlWrongModeMapping:
    def test_wrong_mode_returns_400_content_url_requires_manifest_mode(
        self, stub_metrics, stub_audit
    ):
        svc = AsyncMock()
        svc.send_message = AsyncMock(
            side_effect=ContentUrlWrongModeError(
                recipient_id="agent-target",
                actual_route="inbox",
            )
        )
        _wire_send(stub_metrics, svc, stub_audit)

        with patch("acn.routes.communication.Message", return_value=MagicMock()):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/send",
                    json=_send_body(),
                )

        assert r.status_code == 400, r.text
        body = r.json()
        assert body["error_code"] == "content_url_requires_manifest_mode"
        assert body["details"]["recipient_id"] == "agent-target"
        assert body["details"]["actual_route"] == "inbox"


# ---------------------------------------------------------------------------
# 4. Content fetch — self-hosted branch (GET /manifest/{mid}/content)
# ---------------------------------------------------------------------------


from acn.routes.dependencies import get_manifest_service  # noqa: E402


def _wire_fetch(manifest_service) -> None:
    from acn.routes.dependencies import get_agent_service

    agent_svc = AsyncMock()
    agent_svc.get_agent = AsyncMock(return_value=MagicMock())
    app.dependency_overrides[get_manifest_service] = lambda: manifest_service
    app.dependency_overrides[get_agent_service] = lambda: agent_svc
    app.dependency_overrides[verify_agent_api_key] = lambda: {"agent_id": "agent-owner"}


class TestFetchContentSelfHosted:
    def test_self_hosted_entry_returns_pointer_without_content(self):
        """When the manifest entry has content_url, the content endpoint must
        return self_hosted=true + url/hash without a ``content`` key."""
        mid = "e" * 32
        entry = ManifestEntry(
            mid=mid,
            sender_id="agent-sender",
            summary="hello",
            ts_ms=1_700_000_000_000,
            content_size=0,
            content_url="https://example.com/payload.json",
            content_hash="sha256:deadbeef",
        )
        ms = AsyncMock()
        ms.get_entry = AsyncMock(return_value=entry)
        ms.fetch_content = AsyncMock()
        _wire_fetch(ms)

        with TestClient(app) as client:
            r = client.get(f"/api/v1/communication/content/{mid}")

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["self_hosted"] is True
        assert body["content_url"] == "https://example.com/payload.json"
        assert body["content_hash"] == "sha256:deadbeef"
        assert "content" not in body
        ms.fetch_content.assert_not_awaited()

    def test_self_hosted_entry_without_hash_omits_content_hash(self):
        mid = "f" * 32
        entry = ManifestEntry(
            mid=mid,
            sender_id="agent-sender",
            summary="hello",
            ts_ms=1_700_000_000_000,
            content_size=0,
            content_url="https://example.com/payload.json",
            content_hash=None,
        )
        ms = AsyncMock()
        ms.get_entry = AsyncMock(return_value=entry)
        ms.fetch_content = AsyncMock()
        _wire_fetch(ms)

        with TestClient(app) as client:
            r = client.get(f"/api/v1/communication/content/{mid}")

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["self_hosted"] is True
        assert "content_hash" not in body

    def test_acn_hosted_entry_returns_content_key(self):
        """Regression: ACN-hosted entries must still return ``content`` as before."""
        mid = "0" * 32
        entry = ManifestEntry(
            mid=mid,
            sender_id="agent-sender",
            summary="hello",
            ts_ms=1_700_000_000_000,
            content_size=42,
            content_url=None,
        )
        ms = AsyncMock()
        ms.get_entry = AsyncMock(return_value=entry)
        ms.fetch_content = AsyncMock(return_value={"text": "hello"})
        _wire_fetch(ms)

        with TestClient(app) as client:
            r = client.get(f"/api/v1/communication/content/{mid}")

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["content"] == {"text": "hello"}
        assert "self_hosted" not in body

    def test_missing_entry_returns_404(self):
        ms = AsyncMock()
        ms.get_entry = AsyncMock(return_value=None)
        _wire_fetch(ms)

        with TestClient(app) as client:
            r = client.get(f"/api/v1/communication/content/{'0' * 32}")

        assert r.status_code == 404, r.text
        body = r.json()
        assert body["error_code"] == "manifest_content_not_found"
