import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated
from uuid import uuid4

import aio_pika
from aio_pika.exceptions import AMQPException
from fastapi import FastAPI, HTTPException, Path, Query, Request
from psycopg import OperationalError
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout
from pydantic import BaseModel, Field, StringConstraints, field_validator


QUEUE = "embeddings.requests"
MAX_BIGINT = 2**63 - 1
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32_768)]


class EmbeddingRequest(BaseModel):
    texts: list[Text] = Field(min_length=1, max_length=32)


class EmbeddingResponse(BaseModel):
    model: str
    revision: str
    dimensions: int
    embeddings: list[list[float]]


class DocumentRequest(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def validate_text(cls, text: str) -> str:
        if not text.strip() or "\x00" in text:
            raise ValueError("Text must contain content and cannot contain NUL characters")
        return text


class DocumentResponse(BaseModel):
    id: int
    created_at: datetime


class DocumentDetail(DocumentResponse):
    text: str


class EmbeddingRPC:
    def __init__(self, channel, replies, timeout: float):
        self.channel = channel
        self.replies = replies
        self.timeout = timeout
        self.pending: dict[str, asyncio.Future] = {}

    async def on_reply(self, message):
        future = self.pending.get(message.correlation_id)
        if future is not None and not future.done():
            future.set_result(message.body)

    async def check_worker(self):
        try:
            async with asyncio.timeout(3):
                # aio-pika caches passive declarations on robust channels.
                queue = await self.channel.declare_queue(QUEUE, durable=True, robust=False)
        except (AMQPException, OSError, TimeoutError) as error:
            raise HTTPException(503, "RabbitMQ is unavailable.") from error
        if not queue.declaration_result.consumer_count:
            raise HTTPException(503, "No embedding worker is ready.")

    async def embed(self, payload: EmbeddingRequest):
        await self.check_worker()
        correlation_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[correlation_id] = future
        try:
            async with asyncio.timeout(self.timeout):
                await self.channel.default_exchange.publish(
                    aio_pika.Message(
                        payload.model_dump_json().encode(),
                        content_type="application/json",
                        correlation_id=correlation_id,
                        reply_to=self.replies.name,
                        expiration=self.timeout,
                    ),
                    routing_key=QUEUE,
                )
                result = json.loads(await future)
        except TimeoutError as error:
            raise HTTPException(504, "Embedding worker response timed out.") from error
        except (AMQPException, OSError) as error:
            raise HTTPException(503, "RabbitMQ is unavailable.") from error
        finally:
            self.pending.pop(correlation_id, None)
            future.cancel()

        if "error" in result:
            raise HTTPException(result["status"], result["error"])
        return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    timeout = float(os.getenv("EMBEDDING_RPC_TIMEOUT", "60"))
    if not 0 < timeout < float("inf"):
        raise ValueError("EMBEDDING_RPC_TIMEOUT must be finite and positive")
    conninfo = make_conninfo(
        host=os.getenv("POSTGRES_HOST", "postgres"),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        connect_timeout=5,
    )
    pool = AsyncConnectionPool(conninfo, min_size=1, max_size=4, timeout=5, open=False)
    async with pool, await aio_pika.connect_robust(os.environ["AMQP_URL"], timeout=5) as connection:
        async with pool.connection() as database:
            await database.execute("SELECT pg_advisory_xact_lock(hashtext('deepsemantic.documents'))")
            await database.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    text text NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now()
                )
            """)
        app.state.database = pool
        channel = await connection.channel(on_return_raises=True)
        await channel.declare_queue(QUEUE, durable=True)
        replies = await channel.declare_queue(f"embeddings.reply.{uuid4().hex}", exclusive=True)
        rpc = EmbeddingRPC(channel, replies, timeout)
        await replies.consume(rpc.on_reply, no_ack=True)
        app.state.rpc = rpc
        yield


app = FastAPI(title="BGE-M3 Embedding Service", lifespan=lifespan)


@app.get("/health")
async def health(request: Request):
    await request.app.state.rpc.check_worker()
    return {"status": "ok"}


@app.post("/embeddings", response_model=EmbeddingResponse)
async def embeddings(payload: EmbeddingRequest, request: Request):
    return await request.app.state.rpc.embed(payload)


@asynccontextmanager
async def document_cursor(request: Request):
    try:
        async with request.app.state.database.connection() as database:
            async with database.cursor(row_factory=dict_row) as cursor:
                yield cursor
    except (OperationalError, PoolTimeout) as error:
        raise HTTPException(503, "PostgreSQL is unavailable.") from error


@app.post("/documents", response_model=DocumentResponse, status_code=201)
async def create_document(payload: DocumentRequest, request: Request):
    async with document_cursor(request) as cursor:
        await cursor.execute(
            "INSERT INTO documents (text) VALUES (%s) RETURNING id, created_at",
            (payload.text,),
        )
        return await cursor.fetchone()


@app.get("/documents/{document_id}", response_model=DocumentDetail)
async def get_document(request: Request, document_id: Annotated[int, Path(ge=1, le=MAX_BIGINT)]):
    async with document_cursor(request) as cursor:
        await cursor.execute("SELECT id, text, created_at FROM documents WHERE id = %s", (document_id,))
        document = await cursor.fetchone()
    if document is None:
        raise HTTPException(404, "Document not found.")
    return document


@app.get("/documents", response_model=list[DocumentDetail])
async def get_documents(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=MAX_BIGINT)] = 0,
):
    async with document_cursor(request) as cursor:
        await cursor.execute(
            "SELECT id, text, created_at FROM documents ORDER BY id LIMIT %s OFFSET %s",
            (limit, offset),
        )
        return await cursor.fetchall()
