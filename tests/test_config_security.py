"""Security-validator tests for Settings.

Covers the C2 fix from the pre-launch security audit: defenses are now
decoupled from ``dev_mode`` so a single misconfigured flag cannot disable
all of them at once.

What we pin down:
- INTERNAL_API_TOKEN is required and must be >= 32 chars, regardless of dev_mode
- CORS_ORIGINS=["*"] is rejected when DEV_MODE=false
- AUTH0_DOMAIN / AUTH0_AUDIENCE are required when DEV_MODE=false
- DEV_MODE=true must bind to a loopback interface (refuses 0.0.0.0 / public IPs)
- The legacy hardcoded ``dev-internal-token-2024`` no longer slips through
  silently as a default
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from acn.config import Settings

_DEFAULT_ENV: dict[str, str] = {
    "INTERNAL_API_TOKEN": "valid-token-that-is-32-chars-long-or-more",
    "DEV_MODE": "true",
    "HOST": "127.0.0.1",
    "CORS_ORIGINS": '["*"]',
}

_ALL_KEYS = (
    "INTERNAL_API_TOKEN",
    "DEV_MODE",
    "HOST",
    "CORS_ORIGINS",
    "AUTH0_DOMAIN",
    "AUTH0_AUDIENCE",
    "HUMAN_OIDC_PROVIDERS_JSON",
)


def _mk_env(monkeypatch: pytest.MonkeyPatch, **overrides: str | None) -> None:
    """Set the minimum env vars to satisfy validation, then apply overrides.

    Keys with ``None`` value are deleted from the environment (used to test
    the "missing" case). Keys absent from both the defaults and overrides
    are cleared, so each test starts from a deterministic baseline.
    """
    final: dict[str, str | None] = dict(_DEFAULT_ENV)
    final.update(overrides)
    for k in _ALL_KEYS:
        v = final.get(k, None)
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


def _build_settings() -> Settings:
    """Construct ``Settings`` ignoring any local .env file.

    The repo ships a dev .env that always sets INTERNAL_API_TOKEN; without
    suppressing it the "missing token" cases would silently pass.
    """
    # ``_env_file`` is a pydantic-settings magic kwarg that overrides the
    # configured env_file at construction time.
    return Settings(_env_file=None)


class TestInternalTokenRequired:
    def test_missing_token_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mk_env(monkeypatch, INTERNAL_API_TOKEN=None)
        with pytest.raises(ValidationError) as exc:
            _build_settings()
        assert "INTERNAL_API_TOKEN" in str(exc.value)

    def test_short_token_fails_in_dev_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mk_env(monkeypatch, INTERNAL_API_TOKEN="too-short")
        with pytest.raises(ValidationError) as exc:
            _build_settings()
        msg = str(exc.value)
        assert "INTERNAL_API_TOKEN" in msg and "32" in msg

    def test_legacy_dev_token_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The historical hardcoded default. It's 22 chars, so the length
        # check now refuses it everywhere — not only in production.
        _mk_env(monkeypatch, INTERNAL_API_TOKEN="dev-internal-token-2024")
        with pytest.raises(ValidationError):
            _build_settings()


class TestDevModeLoopbackOnly:
    def test_dev_mode_with_localhost_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mk_env(monkeypatch, HOST="localhost")
        s = _build_settings()
        assert s.dev_mode is True
        assert s.host == "localhost"

    def test_dev_mode_with_127_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mk_env(monkeypatch, HOST="127.0.0.1")
        _build_settings()  # no raise

    def test_dev_mode_with_ipv6_loopback_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mk_env(monkeypatch, HOST="::1")
        _build_settings()  # no raise

    def test_dev_mode_refuses_0_0_0_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mk_env(monkeypatch, HOST="0.0.0.0")
        with pytest.raises(ValidationError) as exc:
            _build_settings()
        assert "DEV_MODE=true" in str(exc.value)
        assert "loopback" in str(exc.value).lower()

    def test_dev_mode_refuses_public_ip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mk_env(monkeypatch, HOST="10.0.0.42")
        with pytest.raises(ValidationError):
            _build_settings()


class TestProductionDefenses:
    """Defenses that activate when DEV_MODE=false."""

    def _prod_env(self, monkeypatch: pytest.MonkeyPatch, **overrides: str | None) -> None:
        prod_defaults: dict[str, str | None] = {
            "DEV_MODE": "false",
            "HOST": "0.0.0.0",
            "CORS_ORIGINS": '["https://example.com"]',
            "AUTH0_DOMAIN": "example.auth0.com",
            "AUTH0_AUDIENCE": "https://api.example.com",
        }
        prod_defaults.update(overrides)
        _mk_env(monkeypatch, **prod_defaults)

    def test_minimal_prod_config_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._prod_env(monkeypatch)
        s = _build_settings()
        assert s.dev_mode is False
        assert s.cors_origins == ["https://example.com"]

    def test_prod_rejects_cors_star(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._prod_env(monkeypatch, CORS_ORIGINS='["*"]')
        with pytest.raises(ValidationError) as exc:
            _build_settings()
        assert "CORS_ORIGINS" in str(exc.value)

    def test_prod_requires_auth0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._prod_env(monkeypatch, AUTH0_DOMAIN=None, AUTH0_AUDIENCE=None)
        with pytest.raises(ValidationError) as exc:
            _build_settings()
        assert "AUTH0_DOMAIN" in str(exc.value) and "AUTH0_AUDIENCE" in str(exc.value)

    def test_prod_can_bind_non_loopback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Binding 0.0.0.0 in production is allowed — only DEV_MODE=true
        # restricts to loopback.
        self._prod_env(monkeypatch, HOST="0.0.0.0")
        s = _build_settings()
        assert s.host == "0.0.0.0"

    def test_prod_token_strength_enforced_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._prod_env(monkeypatch, INTERNAL_API_TOKEN="short")
        with pytest.raises(ValidationError):
            _build_settings()


class TestHumanOidcProviderRegistry:
    """Validation of the pluggable human OIDC provider registry."""

    _GOOD = (
        '[{"name":"cn-wechat","issuer":"https://mp.acnlabs.cn/u",'
        '"audience":"https://api.acnlabs.cn",'
        '"jwks_url":"http://bff:8800/u/.well-known/jwks.json","sub_prefix":"wechat|"}]'
    )

    def _prod_env(self, monkeypatch: pytest.MonkeyPatch, **overrides: str | None) -> None:
        prod_defaults: dict[str, str | None] = {
            "DEV_MODE": "false",
            "HOST": "0.0.0.0",
            "CORS_ORIGINS": '["https://example.com"]',
        }
        prod_defaults.update(overrides)
        _mk_env(monkeypatch, **prod_defaults)

    def test_cn_only_provider_satisfies_prod(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No Auth0 — a region may run with only a domestic provider.
        self._prod_env(monkeypatch, HUMAN_OIDC_PROVIDERS_JSON=self._GOOD)
        s = _build_settings()
        assert s.human_oidc_providers_json == self._GOOD

    def test_malformed_json_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._prod_env(monkeypatch, HUMAN_OIDC_PROVIDERS_JSON="not json{")
        with pytest.raises(ValidationError) as exc:
            _build_settings()
        assert "HUMAN_OIDC_PROVIDERS_JSON" in str(exc.value)

    def test_provider_missing_audience_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._prod_env(
            monkeypatch,
            HUMAN_OIDC_PROVIDERS_JSON='[{"issuer":"https://x/u","jwks_url":"http://x/jwks"}]',
        )
        with pytest.raises(ValidationError) as exc:
            _build_settings()
        assert "issuer, audience" in str(exc.value)

    def test_registry_issuer_shadowing_auth0_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bad = (
            '[{"name":"shadow","issuer":"https://example.auth0.com",'
            '"audience":"a","jwks_url":"http://x/jwks","sub_prefix":"wechat|"}]'
        )
        self._prod_env(
            monkeypatch,
            AUTH0_DOMAIN="example.auth0.com",
            AUTH0_AUDIENCE="https://api.example.com",
            HUMAN_OIDC_PROVIDERS_JSON=bad,
        )
        with pytest.raises(ValidationError) as exc:
            _build_settings()
        assert "shadow Auth0" in str(exc.value)

    def test_missing_sub_prefix_with_auth0_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        no_prefix = (
            '[{"name":"cn","issuer":"https://mp.acnlabs.cn/u",'
            '"audience":"https://api.acnlabs.cn","jwks_url":"http://bff:8800/jwks"}]'
        )
        self._prod_env(
            monkeypatch,
            AUTH0_DOMAIN="example.auth0.com",
            AUTH0_AUDIENCE="https://api.example.com",
            HUMAN_OIDC_PROVIDERS_JSON=no_prefix,
        )
        with pytest.raises(ValidationError) as exc:
            _build_settings()
        assert "sub_prefix" in str(exc.value)

    def test_registry_issuer_shadowing_agent_jwt_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No AGENT_JWT_ISSUER set → effective agent issuer is gateway_base_url
        # default (https://api.acnlabs.dev). A registry provider must not use it.
        bad = (
            '[{"name":"clash","issuer":"https://api.acnlabs.dev",'
            '"audience":"a","jwks_url":"http://x/jwks","sub_prefix":"wechat|"}]'
        )
        self._prod_env(monkeypatch, HUMAN_OIDC_PROVIDERS_JSON=bad)
        with pytest.raises(ValidationError) as exc:
            _build_settings()
        assert "ACN agent JWT issuer" in str(exc.value)

    def test_auth0_plus_distinct_provider_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._prod_env(
            monkeypatch,
            AUTH0_DOMAIN="example.auth0.com",
            AUTH0_AUDIENCE="https://api.example.com",
            HUMAN_OIDC_PROVIDERS_JSON=self._GOOD,
        )
        _build_settings()  # no raise


class TestNoLegacyDefault:
    def test_dev_internal_token_constant_removed(self) -> None:
        """Regression guard: the historical hardcoded default token must
        not exist as a module-level constant. If someone reintroduces it,
        the audit C2 finding is back.
        """
        from acn import config as config_module

        assert not hasattr(config_module, "_DEV_INTERNAL_TOKEN"), (
            "_DEV_INTERNAL_TOKEN was removed in the C2 security audit fix; "
            "re-introducing it would restore an open-source default password."
        )
