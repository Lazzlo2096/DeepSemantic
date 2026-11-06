import os

from sqlalchemy import BigInteger, Column, DateTime, Identity, MetaData, Table, Text, func
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


metadata = MetaData()
documents = Table(
    "documents",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("text", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("qdrant_collection", Text),
    Column("qdrant_point_id", BigInteger),
)


def create_database_engine() -> AsyncEngine:
    url = URL.create(
        "postgresql+psycopg",
        host=os.getenv("POSTGRES_HOST", "postgres"),
        database=os.environ["POSTGRES_DB"],
        username=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )
    return create_async_engine(
        url,
        connect_args={"connect_timeout": 5},
        pool_size=4,
        max_overflow=0,
        pool_timeout=5,
        pool_pre_ping=True,
    )
