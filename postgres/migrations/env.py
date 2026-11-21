import os

from alembic import context
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool


url = URL.create(
    "postgresql+psycopg",
    host=os.getenv("POSTGRES_HOST", "postgres"),
    database=os.environ["POSTGRES_DB"],
    username=os.environ["POSTGRES_USER"],
    password=os.environ["POSTGRES_PASSWORD"],
)
version_table = "auth_alembic_version" if context.config.config_ini_section == "auth" else "alembic_version"

if context.is_offline_mode():
    context.configure(dialect_name="postgresql", literal_binds=True, version_table=version_table)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=NullPool, connect_args={"connect_timeout": 5})
    with engine.begin() as connection:
        connection.execute(select(func.pg_advisory_xact_lock(func.hashtext("deepsemantic.documents"))))
        context.configure(connection=connection, version_table=version_table)
        context.run_migrations()
