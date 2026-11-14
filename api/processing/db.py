import os
from contextlib import asynccontextmanager

import psycopg
from psycopg.rows import dict_row


@asynccontextmanager
async def connection():
    async with await psycopg.AsyncConnection.connect(
        host=os.getenv("POSTGRES_HOST", "postgres"), dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"],
        connect_timeout=5, autocommit=True, row_factory=dict_row,
    ) as db:
        yield db
