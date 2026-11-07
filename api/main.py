import asyncio
import json
import os
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import date, datetime
from typing import Annotated
from urllib.parse import quote
from uuid import uuid4

import aio_pika
import httpx
from aio_pika.exceptions import AMQPException
from fastapi import FastAPI, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import async_sessionmaker

from database import Document, create_database_engine


QUEUE = "embeddings.requests"
ARXIV_QUEUE = "arxiv.imports"
ARXIV_QUEUE_ARGUMENTS = {"x-single-active-consumer": True, "x-consumer-timeout": 86_400_000}
QDRANT_COLLECTION = "documents_bge_m3"
MAX_BIGINT = 2**63 - 1
DocumentId = Annotated[int, Path(ge=1, le=MAX_BIGINT)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32_768)]


class EmbeddingRequest(BaseModel):
    texts: list[Text] = Field(min_length=1, max_length=32)


class EmbeddingResponse(BaseModel):
    model: str
    revision: str
    dimensions: int
    embeddings: list[list[float]]


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


class DocumentRequest(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def validate_text(cls, text: str) -> str:
        if not text.strip() or "\x00" in text:
            raise ValueError("Text must contain content and cannot contain NUL characters")
        return text


class DocumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


class DocumentDetail(DocumentResponse):
    text: str
    qdrant_collection: str | None
    qdrant_point_id: int | None


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
        app.state.document_sessions = async_sessionmaker(engine, expire_on_commit=False)
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


@asynccontextmanager
async def document_session(request: Request):
    try:
        async with request.app.state.document_sessions.begin() as session:
            yield session
    except (OperationalError, PoolTimeout) as error:
        raise HTTPException(503, "PostgreSQL is unavailable.") from error


@app.post("/documents", response_model=DocumentResponse, status_code=201)
async def create_document(payload: DocumentRequest, request: Request):
    async with document_session(request) as session:
        document = Document(text=payload.text)
        session.add(document)
    return document


async def read_document(request: Request, document_id: int):
    async with document_session(request) as session:
        document = await session.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "Document not found.")
    return document


@app.get("/documents/{document_id}", response_model=DocumentDetail)
async def get_document(request: Request, document_id: DocumentId):
    return await read_document(request, document_id)


async def upsert_document_embedding(qdrant: httpx.AsyncClient, document_id: int, result):
    collection_path = f"/collections/{QDRANT_COLLECTION}"
    point = {
        "id": document_id,
        "vector": result["embeddings"][0],
        "payload": {
            "document_id": document_id,
            "model": result["model"],
            "model_revision": result["revision"],
        },
    }
    try:
        response = await qdrant.put(f"{collection_path}/points?wait=true", json={"points": [point]})
        if response.status_code == 404:
            created = await qdrant.put(
                collection_path,
                json={"vectors": {"size": result["dimensions"], "distance": "Cosine"}},
            )
            if created.status_code != 409:
                created.raise_for_status()
            response = await qdrant.put(f"{collection_path}/points?wait=true", json={"points": [point]})
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise HTTPException(503, "Could not save the embedding to Qdrant.") from error
    if response.json().get("result", {}).get("status") != "completed":
        raise HTTPException(503, "Qdrant did not confirm the embedding write.")


@app.get("/documents/{document_id}/embedding", response_model=list[float])
async def get_document_embedding(request: Request, document_id: DocumentId):
    document = await read_document(request, document_id)
    if document.qdrant_collection is None or document.qdrant_point_id is None:
        raise HTTPException(404, "Document embedding not found.")

    collection = quote(document.qdrant_collection, safe="")
    try:
        response = await request.app.state.qdrant.get(
            f"/collections/{collection}/points/{document.qdrant_point_id}"
        )
        if response.status_code == 404:
            raise HTTPException(404, "Document embedding not found.")
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise HTTPException(503, "Could not read the embedding from Qdrant.") from error

    vector = response.json()["result"].get("vector")
    if not isinstance(vector, list):
        raise HTTPException(502, "Qdrant did not return a dense embedding.")
    return vector


@app.post("/documents/{document_id}/embedding", response_model=list[float])
async def embed_document(request: Request, document_id: DocumentId):
    document = await read_document(request, document_id)
    try:
        payload = EmbeddingRequest(texts=[document.text])
    except ValidationError as error:
        raise HTTPException(422, "Stored document text does not meet embedding input limits.") from error
    result = await request.app.state.rpc.embed(payload)
    await upsert_document_embedding(request.app.state.qdrant, document_id, result)
    async with document_session(request) as session:
        updated_id = await session.scalar(
            update(Document)
            .where(Document.id == document_id)
            .values(qdrant_collection=QDRANT_COLLECTION, qdrant_point_id=document_id)
            .returning(Document.id)
        )
        if updated_id is None:
            raise HTTPException(404, "Document not found.")
    return result["embeddings"][0]


@app.get("/documents", response_model=list[DocumentDetail])
async def get_documents(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=MAX_BIGINT)] = 0,
):
    async with document_session(request) as session:
        result = await session.scalars(
            select(Document).order_by(Document.id).limit(limit).offset(offset)
        )
        return result.all()
