"""M13 security tests: startup TLS configuration check.

What this pins down
-------------------
``check_tls_config`` is the operator-facing canary: when ACN starts up
with a plain-HTTP URL on a setting that should be HTTPS in production,
we want a *visible* structured log entry — not silent acceptance.

The rules we pin:

1. Dev mode → completely silent (no warnings).
2. Production (``dev_mode=False``) + non-loopback HTTP URL → warning
   with stable shape (``tls_plaintext_url``, ``setting``, ``url``).
3. Production + loopback HTTP URL → silent (in-pod sidecar pattern).
4. HTTPS / unset URLs → silent.
5. Multiple offenders → multiple warnings, one per setting, no de-dup.

Tests use ``types.SimpleNamespace`` for the settings shim and a tiny
fake logger that records call args — same pattern used elsewhere in
the suite (``test_audit_fire_and_forget`` etc.).
"""

from __future__ import annotations

from types import SimpleNamespace

from acn.security.tls_check import _is_loopback, _is_plain_http, check_tls_config

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────


class _RecordingLogger:
    """Captures structured warning calls; no-ops everything else.

    Mirrors the structlog interface enough that ``check_tls_config`` is
    happy. We deliberately don't depend on structlog itself in tests
    so they stay fast and import-cheap.
    """

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict]] = []

    def warning(self, event: str, **kwargs) -> None:
        self.warnings.append((event, kwargs))

    # ``check_tls_config`` only calls ``warning``; the rest of the
    # logger surface is unused. Provide the bare minimum so accidental
    # additions in the future don't crash the test fixture.
    def info(self, *_a, **_kw) -> None:  # pragma: no cover
        pass

    def error(self, *_a, **_kw) -> None:  # pragma: no cover
        pass


def _settings(**overrides) -> SimpleNamespace:
    """Build a minimal settings stub with the defaults we expect."""
    base = {
        "dev_mode": False,
        "gateway_base_url": "https://api.example.com",
        "frontend_base_url": "https://app.example.com",
        "webhook_url": None,
        "billing_webhook_url": None,
        "backend_url": "https://backend.example.com",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ─────────────────────────────────────────────
# Predicate helpers (small but worth pinning)
# ─────────────────────────────────────────────


class TestPredicates:
    def test_is_plain_http_true_for_http_scheme(self):
        assert _is_plain_http("http://example.com") is True

    def test_is_plain_http_false_for_https(self):
        assert _is_plain_http("https://example.com") is False

    def test_is_plain_http_false_for_empty(self):
        assert _is_plain_http(None) is False
        assert _is_plain_http("") is False

    def test_is_plain_http_false_for_other_schemes(self):
        # ``ws://`` / ``wss://`` aren't in scope for this checker — we
        # only audit HTTPS-able settings.
        assert _is_plain_http("wss://example.com") is False
        assert _is_plain_http("ws://example.com") is False

    def test_is_loopback_recognises_localhost_aliases(self):
        assert _is_loopback("http://localhost:8080") is True
        assert _is_loopback("http://127.0.0.1:8080") is True
        assert _is_loopback("http://[::1]:8080") is True

    def test_is_loopback_false_for_non_loopback(self):
        assert _is_loopback("http://example.com") is False
        assert _is_loopback("http://10.0.0.1") is False


# ─────────────────────────────────────────────
# Dev mode: silent
# ─────────────────────────────────────────────


class TestDevModeIsSilent:
    """Even with HTTP everywhere, dev mode emits nothing — dev rigs run
    plain HTTP by design and warning would be noise."""

    def test_dev_mode_with_all_plain_http(self):
        s = _settings(
            dev_mode=True,
            gateway_base_url="http://localhost:8000",
            frontend_base_url="http://localhost:3000",
            backend_url="http://localhost:9000",
            webhook_url="http://example.com/hook",
            billing_webhook_url="http://example.com/billing",
        )
        log = _RecordingLogger()
        flagged = check_tls_config(s, log)
        assert flagged == []
        assert log.warnings == []


# ─────────────────────────────────────────────
# Production: HTTPS / loopback / unset → silent
# ─────────────────────────────────────────────


class TestProductionSilentCases:
    def test_all_https_no_warnings(self):
        s = _settings()
        log = _RecordingLogger()
        flagged = check_tls_config(s, log)
        assert flagged == []
        assert log.warnings == []

    def test_unset_optional_urls_no_warnings(self):
        # webhook_url / billing_webhook_url default to None — that's an
        # explicit "feature disabled", not a misconfiguration.
        s = _settings(webhook_url=None, billing_webhook_url=None)
        log = _RecordingLogger()
        flagged = check_tls_config(s, log)
        assert flagged == []

    def test_loopback_http_no_warnings(self):
        # In-pod sidecar pattern: backend talks to ACN over the loopback
        # interface. Plain HTTP is fine here, no transit risk.
        s = _settings(backend_url="http://127.0.0.1:9000")
        log = _RecordingLogger()
        flagged = check_tls_config(s, log)
        assert flagged == []

    def test_https_urls_with_paths_no_warnings(self):
        # Trailing path segments shouldn't confuse the scheme check.
        s = _settings(
            webhook_url="https://hooks.example.com/api/v1/incoming",
            billing_webhook_url="https://hooks.example.com/billing/v1",
        )
        log = _RecordingLogger()
        flagged = check_tls_config(s, log)
        assert flagged == []


# ─────────────────────────────────────────────
# Production: plain-HTTP non-loopback → warning
# ─────────────────────────────────────────────


class TestProductionWarnings:
    def test_gateway_base_url_plain_http_warned(self):
        s = _settings(gateway_base_url="http://api.example.com")
        log = _RecordingLogger()
        flagged = check_tls_config(s, log)
        assert flagged == ["gateway_base_url"]
        assert len(log.warnings) == 1
        event, kwargs = log.warnings[0]
        assert event == "tls_plaintext_url"
        assert kwargs["setting"] == "gateway_base_url"
        assert kwargs["url"] == "http://api.example.com"
        # Description should be present (operators read this in dashboards).
        assert kwargs.get("description")
        # Advice text should hint at the fix without dictating it.
        assert "HTTPS" in kwargs.get("advice", "")

    def test_webhook_url_plain_http_warned(self):
        s = _settings(webhook_url="http://hooks.example.com/in")
        log = _RecordingLogger()
        flagged = check_tls_config(s, log)
        assert flagged == ["webhook_url"]

    def test_backend_url_plain_http_warned(self):
        # Backend URL carries the X-Internal-Token. Plain HTTP across
        # hosts is the highest-impact leak we want operators to see.
        s = _settings(backend_url="http://internal-backend.private")
        log = _RecordingLogger()
        flagged = check_tls_config(s, log)
        assert flagged == ["backend_url"]

    def test_multiple_offenders_warn_independently(self):
        # No de-dup, no early return: every misconfigured URL gets its
        # own log line so an alert rule can fire per setting.
        s = _settings(
            gateway_base_url="http://api.example.com",
            webhook_url="http://hooks.example.com/in",
            backend_url="http://internal-backend.private",
        )
        log = _RecordingLogger()
        flagged = check_tls_config(s, log)
        assert sorted(flagged) == sorted(
            ["gateway_base_url", "webhook_url", "backend_url"]
        )
        assert len(log.warnings) == 3
        # Each warning carries the matching setting name.
        settings_seen = sorted(kw["setting"] for _e, kw in log.warnings)
        assert settings_seen == sorted(
            ["gateway_base_url", "webhook_url", "backend_url"]
        )

    def test_frontend_base_url_falls_back_silent_when_unset(self):
        # ``frontend_base_url`` is optional; when unset (None) it's not a
        # misconfiguration — the gateway URL is used in its place.
        s = _settings(frontend_base_url=None)
        log = _RecordingLogger()
        flagged = check_tls_config(s, log)
        assert flagged == []


# ─────────────────────────────────────────────
# Robustness: malformed values don't crash
# ─────────────────────────────────────────────


class TestRobustness:
    def test_garbage_url_string_does_not_raise(self):
        # A misformatted URL should NOT prevent startup — we want the
        # checker to be defensive even against operator typos.
        s = _settings(gateway_base_url="not a url at all")
        log = _RecordingLogger()
        # No exception, no warning (no ``http`` scheme).
        flagged = check_tls_config(s, log)
        assert flagged == []
        assert log.warnings == []

    def test_missing_attributes_do_not_raise(self):
        # If a future setting is added/removed and the attribute is
        # absent, ``getattr(..., None)`` keeps us alive.
        s = SimpleNamespace(dev_mode=False)
        log = _RecordingLogger()
        flagged = check_tls_config(s, log)
        assert flagged == []
