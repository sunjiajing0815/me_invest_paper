import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from alembic.ddl.impl import DefaultImpl
from sqlalchemy import create_engine, pool

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from investor.models import Base  # noqa: E402

# duckdb-engine does not ship an Alembic DDLImpl — register a minimal stub
# so Alembic can manage revision history. Standard CREATE/ALTER DDL works fine;
# batch_alter_table is needed for column renames/type changes.
class DuckDBImpl(DefaultImpl):
    __dialect__ = "duckdb"


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    return "duckdb:///" + os.environ.get("DUCKDB_PATH", "./data/investor.duckdb")


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(get_url(), poolclass=pool.NullPool, future=True)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
