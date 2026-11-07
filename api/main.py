import asyncio
import json
import os
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import date
from uuid import uuid4

import aio_pika
import httpx
from aio_pika.exceptions import AMQPException
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import async_sessionmaker

from chunks import router as chunks_router
from database import create_database_engine
from embedding_schemas import EmbeddingRequest, EmbeddingResponse


QUEUE = "embeddings.requests"
ARXIV_QUEUE = "arxiv.imports"
ARXIV_QUEUE_ARGUMENTS = {"x-single-active-consumer": True, "x-consumer-timeout": 86_400_000}


class ArxivImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_from: date
    date_to: date
    bucket: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    query: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_dates(self):
        if self.date_from > self.date_to:
            raise ValueError("date_from must be on or before date_to")
        return self


class ArxivImportAccepted(BaseModel):
    id: str


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
        app.state.chunk_sessions = async_sessionmaker(engine, expire_on_commit=False)
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
        arxiv_channel = await connection.channel(on_return_raises=True)
        await arxiv_channel.declare_queue(ARXIV_QUEUE, durable=True, arguments=ARXIV_QUEUE_ARGUMENTS)
        app.state.arxiv_channel = arxiv_channel
        yield


app = FastAPI(title="BGE-M3 Embedding Service", lifespan=lifespan)
app.include_router(chunks_router)


@app.get("/health")
async def health(request: Request):
    await request.app.state.rpc.check_worker()
    return {"status": "ok"}


@app.post("/embeddings", response_model=EmbeddingResponse)
async def embeddings(payload: EmbeddingRequest, request: Request):
    return await request.app.state.rpc.embed(payload)


@app.post("/arxiv/imports", response_model=ArxivImportAccepted, status_code=202, tags=["arXiv"])
async def enqueue_arxiv_import(payload: ArxivImportRequest, request: Request):
    job_id = uuid4().hex
    try:
        async with asyncio.timeout(5):
            await request.app.state.arxiv_channel.default_exchange.publish(
                aio_pika.Message(
                    payload.model_dump_json(exclude_none=True).encode(),
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    message_id=job_id,
                ),
                routing_key=ARXIV_QUEUE,
                mandatory=True,
            )
    except (AMQPException, OSError, TimeoutError) as error:
        raise HTTPException(503, "Could not confirm publication of the arXiv import.") from error
    return ArxivImportAccepted(id=job_id)
