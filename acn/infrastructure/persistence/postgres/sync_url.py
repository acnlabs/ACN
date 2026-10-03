"""Sync database URL for Alembic.

SQLAlchemy 2.1 treats a bare ``postgresql://`` URL as the psycopg v3
driver. This project installs psycopg2, so Alembic has to name that
driver or ``alembic upgrade`` exits before uvicorn binds ``/health``.
"""


def sync_migration_url(database_url: str) -> str:
    """Return a sync psycopg2 URL.

    Accepts Railway ``postgres://``, asyncpg ``postgresql+asyncpg://``,
    and a bare ``postgresql://``. A URL that already names another
    driver is left unchanged.
    """
    url = database_url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql+asyncpg://"):
        url = "postgresql://" + url[len("postgresql+asyncpg://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://") :]
    return url
