"""REST configuration and independently observable pipeline branches."""
from hashlib import md5
from typing import Literal
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from temporalio.exceptions import TemporalError

from auth import AdminUser
from database import database_session
from papers import ArxivId
import paper_repository as repository
from processing.config import pipeline_settings, summary_generation, summary_workflow_id
import temporal_runtime

RECONCILER_ID = "pipeline:summary-reconciler"
router = APIRouter(tags=["pipeline"])


class PipelineSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary_mode: Literal["enabled", "deferred"]


class PipelineSettingsResponse(PipelineSettingsUpdate):
    updated_at: datetime
    reconciler_workflow_id: str = RECONCILER_ID


class IndexProgress(BaseModel):
    status: Literal["pending", "queued", "running", "completed", "failed", "deferred"]
    chunks_total: int = Field(ge=0)
    chunks_indexed: int = Field(ge=0)


class SummaryIndexProgress(IndexProgress):
    summary_available: bool
    runtime: dict | None


class PaperProcessingResponse(BaseModel):
    arxiv_id: str
    markdown: Literal["pending", "completed"]
    article_index: IndexProgress
    summary_mode: Literal["enabled", "deferred"]
    summary_index: SummaryIndexProgress


@router.get("/pipeline/settings", response_model=PipelineSettingsResponse)
async def get_settings(_: AdminUser):
    return await repository.get_pipeline_settings()


@router.put("/pipeline/settings", response_model=PipelineSettingsResponse)
async def update_settings(payload: PipelineSettingsUpdate, request: Request, _: AdminUser):
    # Start before committing configuration: a lost HTTP response or signal cannot
    # leave an enabled configuration without a durable periodic catalog scan.
    handle = await temporal_runtime.start(request, "SummaryReconcileWorkflow", {}, RECONCILER_ID)
    async with database_session(request) as session:
        row = (await session.execute(text("""
            UPDATE pipeline_settings SET summary_mode = :mode, updated_at = now()
            WHERE singleton RETURNING summary_mode, updated_at
        """), {"mode": payload.summary_mode})).mappings().one()
    try:
        await handle.signal("wake", rpc_timeout=timedelta(seconds=3))
    except TemporalError:
        # The workflow also wakes on a timer and reads the committed setting.
        pass
    return dict(row)


@router.get("/pipeline/summary-reconciliation")
async def reconciliation_progress(request: Request, _: AdminUser):
    return {"workflow_id": RECONCILER_ID, "runtime": await temporal_runtime.describe(request, RECONCILER_ID)}


@router.get("/papers/{arxiv_id:path}/processing", response_model=PaperProcessingResponse)
async def paper_processing(request: Request, arxiv_id: ArxivId):
    paper = await repository.get_paper(arxiv_id)
    if paper is None:
        raise HTTPException(404, "Paper not found.")
    settings = pipeline_settings()
    mode = (await repository.get_pipeline_settings())["summary_mode"]
    article = {"status": "pending", "chunks_total": 0, "chunks_indexed": 0}
    summary_index = dict(article)
    cached = None
    runtime = None
    if paper["text_object_key"]:
        article = await repository.index_progress({"arxiv_id": arxiv_id, "source_kind": "article",
            "source_version": paper["markdown_version"]}, settings)
        cached = await repository.get_summary(arxiv_id, generation=summary_generation(paper, settings))
        if cached:
            summary_index = await repository.index_progress({"arxiv_id": arxiv_id, "source_kind": "summary",
                "source_version": md5(cached["summary"].encode()).hexdigest()}, settings)
        if summary_index["status"] != "completed":
            runtime = await temporal_runtime.describe(request, summary_workflow_id(paper, settings))
    status = summary_index["status"]
    if status != "completed":
        status = "deferred" if mode == "deferred" else (runtime or {}).get("status", "pending")
        # A closed run may have deferred or become superseded; only artifacts
        # establish end-to-end completion.
        if status == "completed":
            status = "pending"
    return {"arxiv_id": arxiv_id, "markdown": "completed" if paper["text_object_key"] else "pending",
            "article_index": article, "summary_mode": mode,
            "summary_index": {**summary_index, "status": status, "summary_available": cached is not None,
                              "runtime": runtime}}
