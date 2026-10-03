"""Alembic must name psycopg2. A bare postgresql:// URL is psycopg v3 on SQLAlchemy 2.1."""

from acn.infrastructure.persistence.postgres.sync_url import sync_migration_url


def test_asyncpg_url_uses_psycopg2() -> None:
    url = "postgresql+asyncpg://acn:secret@db.internal:5432/acn"
    assert sync_migration_url(url) == "postgresql+psycopg2://acn:secret@db.internal:5432/acn"


def test_railway_postgres_scheme_uses_psycopg2() -> None:
    url = "postgres://acn:secret@db.internal:5432/acn"
    assert sync_migration_url(url) == "postgresql+psycopg2://acn:secret@db.internal:5432/acn"


def test_bare_postgresql_url_uses_psycopg2() -> None:
    url = "postgresql://acn:secret@db.internal:5432/acn?sslmode=require"
    assert (
        sync_migration_url(url)
        == "postgresql+psycopg2://acn:secret@db.internal:5432/acn?sslmode=require"
    )


def test_explicit_driver_is_left_unchanged() -> None:
    url = "postgresql+psycopg2://acn:secret@db.internal:5432/acn"
    assert sync_migration_url(url) == url
