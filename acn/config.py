"""
ACN Configuration

Settings for ACN service
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Gateway
    gateway_base_url: str = "http://localhost:8000"

    # Webhooks (for backend integration)
    webhook_url: str | None = None  # e.g., "https://your-backend.com/api/acn/webhook"
    webhook_secret: str | None = None  # For HMAC signature verification
    webhook_timeout: int = 30  # seconds
    webhook_retry_count: int = 3
    webhook_retry_delay: int = 5  # seconds

    # PostgreSQL (for future persistent storage)
    database_url: str | None = None

    # CORS
    cors_origins: list[str] = ["*"]

    # Logging
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance"""
    return Settings()
