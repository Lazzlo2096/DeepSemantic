from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import text

from database import ArxivPaper, database_session
from papers import ArxivId
from summary_schema import MARKDOWN_REQUIRED, PaperSummary


router = APIRouter(prefix="/papers", tags=["papers"])

class SummaryJob(BaseModel):
    id: UUID
    arxiv_id: str
    status: Literal["queued", "running", "completed", "failed"]
    parts_completed: int
    parts_total: int
    model: str | None
    prompt_version: str | None
    summary: PaperSummary | None
    last_error: str | None
    finished_at: datetime | None


JOB_FIELDS = """
    id, arxiv_id, status, parts_completed, parts_total, model,
    prompt_version, summary, last_error, finished_at
"""
SELECT_JOB = text(f"SELECT {JOB_FIELDS} FROM paper_summaries WHERE arxiv_id = :arxiv_id")


@router.get("/{arxiv_id:path}/summary", response_model=SummaryJob | None)
async def get_paper_summary(request: Request, arxiv_id: ArxivId):
    async with database_session(request) as session:
        if await session.get(ArxivPaper, arxiv_id) is None:
            raise HTTPException(404, "Paper not found.")
        return (await session.execute(SELECT_JOB, {"arxiv_id": arxiv_id})).mappings().one_or_none()


@router.post("/{arxiv_id:path}/summary", status_code=202, response_model=SummaryJob, responses={200: {"model": SummaryJob}})
async def create_paper_summary(request: Request, response: Response, arxiv_id: ArxivId):
    async with database_session(request) as session:
        paper = await session.get(ArxivPaper, arxiv_id)
        if paper is None:
            raise HTTPException(404, "Paper not found.")
        params = {"arxiv_id": arxiv_id}
        if not paper.text_object_key or not paper.text_object_key.lower().endswith(".md"):
            job = (await session.execute(SELECT_JOB, params)).mappings().one_or_none()
            if job is not None and job["status"] == "completed":
                response.status_code = 200
                return job
            raise HTTPException(409, MARKDOWN_REQUIRED)

        job = (await session.execute(text(f"""
            INSERT INTO paper_summaries (arxiv_id) VALUES (:arxiv_id)
            ON CONFLICT (arxiv_id) DO UPDATE
            SET id = EXCLUDED.id, status = 'queued', attempts = 0,
                available_at = now(), created_at = now(), finished_at = NULL,
                parts_completed = 0, parts_total = 0, last_error = NULL,
                model = NULL, prompt_version = NULL
            WHERE paper_summaries.status = 'failed'
            RETURNING {JOB_FIELDS}
        """), params)).mappings().one_or_none()
        if job is None:
            job = (await session.execute(SELECT_JOB, params)).mappings().one()
        if job["status"] == "completed":
            response.status_code = 200
        return job
