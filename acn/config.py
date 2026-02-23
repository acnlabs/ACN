"""
ACN Configuration

Settings for ACN service
"""

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_INTERNAL_TOKEN = "dev-internal-token-2024"


class Settings(BaseSettings):
    """ACN Settings"""

    # Service
    service_name: str = "ACN"
    service_version: str = "0.1.0"
    host: str = "0.0.0.0"
    port: int = 8000

    # Redis
    redis_url: str = "redis://localhost:6379"
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str | None = None

    # A2A Protocol
    a2a_protocol_version: str = "0.3.0"

    # Gateway
    gateway_base_url: str = "http://localhost:8000"

    # Backend URL (for escrow and other integrations)
    backend_url: str = "http://localhost:8000"

    # Internal API Token (shared with Backend for service-to-service auth)
    internal_api_token: str = _DEV_INTERNAL_TOKEN

    # Webhooks (for backend integration)
    webhook_url: str | None = None  # e.g., "https://your-backend.com/api/acn/webhook"
    webhook_secret: str | None = None  # For HMAC signature verification
    webhook_timeout: int = 30  # seconds
    webhook_retry_count: int = 3
    webhook_retry_delay: int = 5  # seconds

    # Billing webhook
    billing_webhook_url: str | None = None  # e.g., "https://your-backend.com/api/billing/webhook"

    # Auth0 (for JWT verification and Agent Card security scheme)
    auth0_domain: str | None = None  # e.g., "your-tenant.auth0.com"
    auth0_audience: str | None = None  # e.g., "https://api.agentplanet.com"

    # PostgreSQL (for future persistent storage)
    database_url: str | None = None

    # CORS
    cors_origins: list[str] = ["*"]

    # Observability
    log_level: str = "INFO"
    otel_enabled: bool = False  # Enable OpenTelemetry (requires opentelemetry-sdk)

    # Development mode (disables Auth0 requirement for some endpoints)
    dev_mode: bool = True  # Set to False in production

    # API docs (Swagger UI / ReDoc / openapi.json)
    # Independent of dev_mode — operators can expose docs on staging while using prod auth
    enable_docs: bool = True  # Set to False on official hosted instances

    # WebSocket limits
    max_websocket_connections: int = 10_000

    # Labs features (experimental)
    labs_onboarding_enabled: bool = True  # Agent self-onboarding experiment

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        """Fail fast on unsafe configuration when running in production mode."""
        if self.dev_mode:
            return self

        errors: list[str] = []

        if not self.internal_api_token or len(self.internal_api_token) < 32:
            errors.append(
                "INTERNAL_API_TOKEN must be at least 32 characters in production."
            )
        elif self.internal_api_token == _DEV_INTERNAL_TOKEN:
            errors.append(
                "INTERNAL_API_TOKEN must be overridden in production "
                "(current value is the insecure development default)."
            )

        if self.cors_origins == ["*"]:
            errors.append(
                "CORS_ORIGINS must not be ['*'] in production. "
                "Set it to the list of allowed origins."
            )

        if not self.auth0_domain or not self.auth0_audience:
            errors.append(
                "AUTH0_DOMAIN and AUTH0_AUDIENCE must be set in production."
            )

        if not self.redis_password:
            errors.append(
                "REDIS_PASSWORD must be set in production. "
                "Running Redis without a password is insecure."
            )

        if self.enable_docs:
            errors.append(
                "ENABLE_DOCS must be False in production. "
                "Set ENABLE_DOCS=false to disable Swagger UI / ReDoc / openapi.json."
            )

        if errors:
            raise ValueError(
                "Production configuration errors detected:\n"
                + "\n".join(f"  - {e}" for e in errors)
            )

        return self


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance"""
    return Settings()
