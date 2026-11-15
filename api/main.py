import asyncio
import json
import os
from contextlib import AsyncExitStack, asynccontextmanager, closing
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from chunks import router as chunks_router
from database import MAX_BIGINT, create_database_engine, database_session
from embedding_schemas import EmbeddingRequest, EmbeddingResponse
from llm_chat import ChatSettings
from embedding_service import create_embeddings
from paper_summaries import router as paper_summaries_router
from papers import router as papers_router
from pipeline import router as pipeline_router
from rag import router as rag_router
from rag_model import create_rag_model
from storage import connect_rustfs
from processing.config import pipeline_settings
import temporal_runtime


class ArxivImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID | None = None
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


class ArxivImportResult(BaseModel):
    articles_found: int | None = None
    pdf_available: int = 0
    pdf_reused: int = 0
    pdf_failures: int = 0
    html_unavailable: int = 0


class ArxivImportJob(BaseModel):
    id: UUID
    date_from: date
    date_to: date
    bucket: str
    query: str
    status: Literal["queued", "running", "pausing", "paused", "completed", "failed", "unknown"]
    attempts: int
    created_at: datetime
    available_at: datetime | None = None
    started_at: datetime | None
    finished_at: datetime | None
    processed_articles: int | None
    result: ArxivImportResult | None

    @field_serializer("id")
    def serialize_id(self, job_id: UUID) -> str:
        return job_id.hex


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncExitStack() as stack:
        engine = create_database_engine()
        stack.push_async_callback(engine.dispose)
        app.state.db_engine = engine
        app.state.db_sessions = async_sessionmaker(engine, expire_on_commit=False)
        app.state.rustfs = stack.enter_context(closing(connect_rustfs())) if os.getenv("RUSTFS_ENDPOINT") else None
        app.state.qdrant = await stack.enter_async_context(
            httpx.AsyncClient(base_url=os.getenv("QDRANT_URL", "http://qdrant:6333"), timeout=5)
        )
        app.state.chat_settings = chat_settings = ChatSettings.from_env(openrouter_model_env="RAG_OPENROUTER_MODEL")
        app.state.chat_client = await stack.enter_async_context(httpx.AsyncClient(
            base_url=chat_settings.base_url,
            timeout=None,
        ))
        app.state.rag_model = create_rag_model(chat_settings, app.state.chat_client) if chat_settings.configured else None
        app.state.embeddings = await stack.enter_async_context(create_embeddings())
        yield


app = FastAPI(title="DeepSemantic API", lifespan=lifespan)
app.include_router(chunks_router)
app.include_router(pipeline_router)
app.include_router(paper_summaries_router)
app.include_router(papers_router)
app.include_router(rag_router)


@app.get("/health/live", include_in_schema=False)
async def live():
    return {"status": "ok"}


@app.get("/health")
async def health(request: Request):
    request.app.state.embeddings.check_configured()
    return {"status": "ok"}


@app.post("/embeddings", response_model=EmbeddingResponse)
async def embeddings(payload: EmbeddingRequest, request: Request):
    return await request.app.state.embeddings.embed(payload)


def import_payload(row):
    return {"id": str(row["id"]), "date_from": str(row["date_from"]), "date_to": str(row["date_to"]),
            "bucket": row["bucket"], "query": row["query"], "source_format": os.getenv("ARXIV_SOURCE_FORMAT", "html"),
            "settings": pipeline_settings()}


@app.post("/arxiv/imports", response_model=ArxivImportAccepted, status_code=202, tags=["arXiv"])
async def enqueue_arxiv_import(payload: ArxivImportRequest, request: Request):
    job_id = payload.request_id or uuid4()
    params = {"id": job_id, "date_from": payload.date_from, "date_to": payload.date_to,
              "bucket": payload.bucket or os.getenv("RUSTFS_BUCKET", "arxiv-papers"), "query": payload.query,
              "workflow_id": f"import:{job_id.hex}"}
    # This is an immutable request catalog, not a job queue. It is never polled by workers.
    async with database_session(request) as session:
        await session.execute(text("""
            INSERT INTO arxiv_imports (id, date_from, date_to, bucket, query, workflow_id)
            VALUES (:id, :date_from, :date_to, :bucket, :query, :workflow_id) ON CONFLICT DO NOTHING
        """), params)
        row = (await session.execute(text("SELECT * FROM arxiv_imports WHERE id = :id"), params)).mappings().one()
        if any(row[key] != params[key] for key in ("date_from", "date_to", "bucket", "query")):
            raise HTTPException(409, "request_id already belongs to a different import.")
    await temporal_runtime.start(request, "ImportWorkflow", import_payload(row), row["workflow_id"])
    print(json.dumps({"event": "arxiv_import.insert", "table": "arxiv_imports", "operation": "insert",
                      "import_id": job_id.hex, "row": dict(row)}, default=str), flush=True)
    return ArxivImportAccepted(id=job_id.hex)


@app.post("/arxiv/imports/{job_id}/{action}", tags=["arXiv"])
async def control_arxiv_import(job_id: UUID, action: Literal["pause", "resume"], request: Request):
    async with database_session(request) as session:
        row = (await session.execute(text("SELECT * FROM arxiv_imports WHERE id = :id"), {"id": job_id})).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, "Задание импорта не найдено.")
    runtime = await temporal_runtime.describe(request, row["workflow_id"])
    if runtime is None or (runtime["status"] == "failed" and action == "resume"):
        if action != "resume":
            raise HTTPException(409, "История Temporal отсутствует; можно заново запустить сохранённый запрос.")
        await temporal_runtime.start(request, "ImportWorkflow", import_payload(row), row["workflow_id"])
        return {"status": "queued"}
    if runtime["status"] in ("completed", "failed"):
        raise HTTPException(409, "Команда недоступна для завершённого задания.")
    await temporal_runtime.signal_pause(request, row["workflow_id"], action == "pause")
    return {"status": "pausing" if action == "pause" else "running"}


@app.get("/arxiv/imports", response_model=list[ArxivImportJob], tags=["arXiv"])
async def list_arxiv_imports(request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=MAX_BIGINT)] = 0):
    async with database_session(request) as session:
        rows = (await session.execute(text("""
            SELECT * FROM arxiv_imports ORDER BY created_at DESC, id DESC LIMIT :limit OFFSET :offset
        """), {"limit": limit, "offset": offset})).mappings().all()
    async def present(row):
        runtime = await temporal_runtime.describe(request, row["workflow_id"])
        progress = runtime["progress"] if runtime else {}
        result = {key: progress.get(key, 0) for key in ("articles_found", "pdf_available", "pdf_reused", "pdf_failures", "html_unavailable")}
        return {**row, "status": runtime["status"] if runtime else "unknown",
                "attempts": progress.get("attempts", 0), "available_at": None,
                "started_at": runtime["started_at"] if runtime else None,
                "finished_at": runtime["finished_at"] if runtime else None,
                "processed_articles": progress.get("processed_articles"), "result": result if progress else None}
    return await asyncio.gather(*(present(row) for row in rows))
