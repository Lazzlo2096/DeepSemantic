import os
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Identity, Text, func
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    qdrant_collection: Mapped[str | None] = mapped_column(Text)
    qdrant_point_id: Mapped[int | None] = mapped_column(BigInteger)


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
