from logging.config import fileConfig
import os

from alembic import context
from sqlalchemy import engine_from_config, pool, text
from dotenv import load_dotenv

load_dotenv()
from app.db import Base
from app import models  # noqa: F401

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
# Alembic is a process-level CLI, so its local default must persist the
# version marker between invocations. Application tests use an in-memory
# database through app.db; production supplies DATABASE_URL explicitly.
config.set_main_option("sqlalchemy.url", os.getenv("DATABASE_URL", "sqlite:///./scribed.db"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        # Remove only version markers from the deleted migration history. The
        # current baseline itself is never reset automatically.
        if connection.dialect.has_table(connection, "alembic_version"):
            current = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
            if current and current != "20260913_current_schema":
                connection.execute(text("DELETE FROM alembic_version"))
                connection.commit()
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
