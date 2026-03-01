"""Alembic migration environment."""

import importlib.util
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# Alembic Config object
config = context.config

# Set up logging from ini file
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# Import ORM Base without triggering the full acn package __init__.py
# (which has circular-import-inducing top-level imports).
# ---------------------------------------------------------------------------
_models_path = (
    Path(__file__).parent.parent / "acn" / "infrastructure" / "persistence" / "postgres" / "models.py"
)
_spec = importlib.util.spec_from_file_location("acn_pg_models", _models_path)
_module = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["acn_pg_models"] = _module
_spec.loader.exec_module(_module)  # type: ignore[union-attr]

target_metadata = _module.Base.metadata

# ---------------------------------------------------------------------------
# Override sqlalchemy.url from DATABASE_URL env var if present.
# alembic needs the *sync* psycopg2/psycopg driver URL for offline migrations.
# ---------------------------------------------------------------------------
database_url = os.environ.get("DATABASE_URL", "")
if database_url:
    sync_url = (
        database_url.replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgres://", "postgresql://")
    )
    config.set_main_option("sqlalchemy.url", sync_url)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection (for dry-run / review)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
