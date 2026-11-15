"""Alembic-miljö. Databas-URL:en hämtas från DRIVELOG_DATABASE_URL."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

import app.models  # noqa: F401 - registrerar alla tabeller
from app.config import get_config
from app.db import SQLITE_BUSY_TIMEOUT_SECONDS, Base, secure_sqlite_file, sqlite_file_path

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)
_url = config.attributes.get("database_url") or get_config().database_url
config.set_main_option("sqlalchemy.url", _url.replace("%", "%%"))
_sqlite_file = sqlite_file_path(_url)
if _sqlite_file is not None:
    # Enprocessläget: en ny installation har ingen databasfil – katalogen skapas här.
    _sqlite_file.parent.mkdir(parents=True, exist_ok=True)
    secure_sqlite_file(_sqlite_file)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        render_as_batch=config.get_main_option("sqlalchemy.url", "").startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connect_args = {"timeout": SQLITE_BUSY_TIMEOUT_SECONDS} if _sqlite_file is not None else {}
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool,
        connect_args=connect_args,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
