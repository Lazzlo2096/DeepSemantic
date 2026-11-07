import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import HTTPException, Request
from sqlalchemy import BigInteger, DateTime, Identity, Text, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


MAX_BIGINT = 2**63 - 1


class Base(DeclarativeBase):
    pass


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    qdrant_collection: Mapped[str | None] = mapped_column(Text)
    qdrant_point_id: Mapped[int | None] = mapped_column(BigInteger)


class ArxivDownload(Base):
    __tablename__ = "arxiv_downloads"

    arxiv_id: Mapped[str] = mapped_column(Text, primary_key=True)
    bucket: Mapped[str] = mapped_column(Text)
    object_key: Mapped[str] = mapped_column(Text)
    text_object_key: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    authors: Mapped[list[str]] = mapped_column(ARRAY(Text))
    abstract: Mapped[str] = mapped_column(Text)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    downloaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


@asynccontextmanager
async def database_session(request: Request):
    try:
        async with request.app.state.db_sessions.begin() as session:
            yield session
    except (OperationalError, PoolTimeout) as error:
        raise HTTPException(503, "PostgreSQL is unavailable.") from error


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
