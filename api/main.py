import asyncio
import json
import os
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

import aio_pika
import httpx
from aio_pika.exceptions import AMQPException
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from chunks import router as chunks_router
from database import MAX_BIGINT, create_database_engine, database_session
from embedding_schemas import EmbeddingRequest, EmbeddingResponse
from paper_summaries import router as paper_summaries_router
from papers import router as papers_router


QUEUE = "embeddings.requests"


class ArxivImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_from: date
    date_to: date
    bucket: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    query: str = Field(default="", max_length=2000, pattern=r"^[^\x00]*$")

    @model_validator(mode="after")
    def validate_dates(self):
        if self.date_from > self.date_to:
            raise ValueError("date_from must be on or before date_to")
        return self


class ArxivImportAccepted(BaseModel):
    id: str


class ArxivImportJob(BaseModel):
    id: UUID
    date_from: date
    date_to: date
    bucket: str
    query: str
    status: Literal["queued", "running", "completed", "failed"]
    attempts: int
    created_at: datetime
    available_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    result: dict[str, int] | None
    last_error: str | None

    @field_serializer("id")
    def serialize_id(self, job_id: UUID) -> str:
        return job_id.hex


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
    async with AsyncExitStack() as stack:
        engine = create_database_engine()
        stack.push_async_callback(engine.dispose)
        app.state.db_engine = engine
        app.state.db_sessions = async_sessionmaker(engine, expire_on_commit=False)
        app.state.qdrant = await stack.enter_async_context(
            httpx.AsyncClient(base_url=os.getenv("QDRANT_URL", "http://qdrant:6333"), timeout=5)
        )
        connection = await stack.enter_async_context(
            await aio_pika.connect_robust(os.environ["AMQP_URL"], timeout=5)
        )
        channel = await connection.channel(on_return_raises=True)
        await channel.declare_queue(QUEUE, durable=True)
        replies = await channel.declare_queue(f"embeddings.reply.{uuid4().hex}", exclusive=True)
        rpc = EmbeddingRPC(channel, replies, timeout)
        await replies.consume(rpc.on_reply, no_ack=True)
        app.state.rpc = rpc
        yield


app = FastAPI(title="BGE-M3 Embedding Service", lifespan=lifespan)
app.include_router(chunks_router)
app.include_router(paper_summaries_router)
app.include_router(papers_router)


@app.get("/health")
async def health(request: Request):
    await request.app.state.rpc.check_worker()
    return {"status": "ok"}


@app.post("/embeddings", response_model=EmbeddingResponse)
async def embeddings(payload: EmbeddingRequest, request: Request):
    return await request.app.state.rpc.embed(payload)


@app.post("/arxiv/imports", response_model=ArxivImportAccepted, status_code=202, tags=["arXiv"])
async def enqueue_arxiv_import(payload: ArxivImportRequest, request: Request):
    parameters = payload.model_dump()
    if payload.bucket is None:
        parameters["bucket"] = os.getenv("RUSTFS_BUCKET", "arxiv-papers")
    async with database_session(request) as session:
        job_id, row = (await session.execute(
            text("""
                INSERT INTO arxiv_imports AS job (date_from, date_to, bucket, query)
                VALUES (:date_from, :date_to, :bucket, :query)
                RETURNING id, to_jsonb(job)
            """),
            parameters,
        )).one()
    print(json.dumps({
        "event": "arxiv.import_queued",
        "table": "arxiv_imports",
        "operation": "insert",
        "import_id": job_id.hex,
        "row": row,
    }), flush=True)
    return ArxivImportAccepted(id=job_id.hex)


@app.get("/arxiv/imports", response_model=list[ArxivImportJob], tags=["arXiv"])
async def list_arxiv_imports(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=MAX_BIGINT)] = 0,
):
    async with database_session(request) as session:
        rows = await session.execute(
            text("""
                SELECT id, date_from, date_to, bucket, query, status, attempts, created_at,
                       available_at, started_at, finished_at, result, last_error
                FROM arxiv_imports
                ORDER BY created_at DESC, id DESC
                LIMIT :limit OFFSET :offset
            """),
            {"limit": limit, "offset": offset},
        )
        return rows.mappings().all()
