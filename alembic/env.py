"""Alembic environment.

Two modes, both required:

* **Offline** (``alembic upgrade head --sql``) renders the migration script to stdout without a
  database connection. This is what makes migration *review* possible: a reviewer reads the
  generated SQL in a pull request, and CI can validate that migrations are renderable without
  provisioning a database.
* **Online** connects using ``KA_DATABASE_URL``.

The DSN is read from the environment and never written to disk. Offline mode substitutes a
placeholder URL so that a migration which accidentally embeds the real DSN in its output still
cannot leak the live credential - a small but real risk, since ``--sql`` output is frequently
pasted into tickets and pull requests.

Also enforced here: a statement timeout on the migration connection. A migration that blocks on a
lock will otherwise hang indefinitely in CI, where nobody is watching.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection

from knowledge_assistant.config.settings import Environment, load_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None

#: PostgreSQL statement timeout for migration sessions, in milliseconds. Migrations are DDL and
#: occasionally rebuild an index; a generous but finite budget prevents an indefinite hang.
_MIGRATION_STATEMENT_TIMEOUT_MS = 60_000

_OFFLINE_PLACEHOLDER_URL = "postgresql+psycopg://offline:offline@localhost/offline"


def _database_url() -> str:
    """Return the DSN from validated settings.

    Returns:
        The PostgreSQL URL, without the ``+psycopg`` driver suffix removed.

    Raises:
        Exception: Propagated from settings validation, so a missing or invalid ``KA_DATABASE_URL``
            fails the migration rather than connecting to the wrong database.

    """
    settings = load_settings()
    return settings.database_url.get_secret_value()


def run_migrations_offline() -> None:
    """Render migrations to SQL on stdout without connecting.

    Uses a placeholder DSN so the rendered script can never contain a live credential.
    """
    context.configure(
        url=_OFFLINE_PLACEHOLDER_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        # Emit the session policy the application relies on, so a schema applied via
        # `alembic upgrade --sql` behaves the same as one applied by the application.
        opts={"executemany": False},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect to the database and apply migrations."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as raw_connection:
        # Bound to a separate name rather than rebinding `raw_connection`, so the object
        # Alembic hands to the migration context is visibly not the pooled connection.
        connection = raw_connection.execution_options(isolation_level="AUTOCOMMIT")
        _apply_session_policy(connection)
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


def _apply_session_policy(connection: Connection) -> None:
    """Apply the timeout policy the application depends on.

    Args:
        connection: Migration connection.

    """
    connection.exec_driver_sql(f"SET statement_timeout = {_MIGRATION_STATEMENT_TIMEOUT_MS}")
    connection.exec_driver_sql("SET lock_timeout = '10s'")


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

del os, Environment
