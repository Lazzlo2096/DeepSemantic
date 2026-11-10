from datetime import UTC, date, datetime, time
from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import quote

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import column, func, select, table, text, true

from database import MAX_BIGINT, PAPER_SUMMARY_STATUS_TYPE, ArxivPaper, PaperStatus, database_session


router = APIRouter(prefix="/papers", tags=["papers"])
ArxivId = Annotated[str, Path(min_length=1, pattern=r"^[^\x00]+$")]
PaperSort = Literal["downloaded_desc", "submitted_desc", "submitted_asc", "status"]
TEXT_MEDIA_TYPES = {".md": "text/markdown", ".txt": "text/plain"}


class PaperResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    arxiv_id: str
    bucket: str
    object_key: str
    text_object_key: str | None
    title: str
    authors: list[str]
    abstract: str
    submitted_at: datetime
    downloaded_at: datetime
    status: PaperStatus


class PaperListItem(PaperResponse):
    has_summary: bool


class PaperChunkResponse(BaseModel):
    chunk_id: int
    chunk_index: int
    text: str


class PaperFileResponse(StreamingResponse):
    def __init__(self, stored_object, media_type: str, filename: str | None):
        self.object_body = stored_object["Body"]
        headers = {
            "Content-Length": str(stored_object["ContentLength"]),
            "X-Content-Type-Options": "nosniff",
        }
        if filename is not None:
            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename, safe='')}"
        super().__init__(self.object_body.iter_chunks(chunk_size=64 * 1024), media_type=media_type, headers=headers)

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.object_body.close()


async def open_paper_object(request: Request, bucket: str, key: str):
    s3 = request.app.state.rustfs
    if s3 is None:
        raise HTTPException(503, "Paper storage is unavailable.")
    try:
        return await run_in_threadpool(s3.get_object, Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response["ResponseMetadata"]["HTTPStatusCode"] == 404:
            raise HTTPException(404, "Paper file not found.") from error
        raise HTTPException(503, "Paper storage is unavailable.") from error
    except BotoCoreError as error:
        raise HTTPException(503, "Paper storage is unavailable.") from error


async def paper_file(request: Request, bucket: str, key: str, media_type: str, filename: str | None = None):
    return PaperFileResponse(await open_paper_object(request, bucket, key), media_type, filename)


def paper_order(paper, sort: PaperSort):
    if sort == "submitted_asc":
        return paper.submitted_at.asc(), paper.arxiv_id
    if sort == "submitted_desc":
        return paper.submitted_at.desc(), paper.arxiv_id
    if sort == "status":
        return paper.status, paper.submitted_at.desc(), paper.arxiv_id
    return paper.downloaded_at.desc(), paper.arxiv_id


@router.get("", response_model=list[PaperListItem])
async def list_papers(
    request: Request,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=MAX_BIGINT)] = 0,
    sort: PaperSort = "downloaded_desc",
    status: PaperStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    has_summary: bool | None = None,
):
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to.")

    summaries = table("paper_summaries", column("arxiv_id"), column("status", PAPER_SUMMARY_STATUS_TYPE))
    summary_ready = (
        select(summaries.c.arxiv_id)
        .where(summaries.c.arxiv_id == ArxivPaper.arxiv_id, summaries.c.status == "completed")
        .correlate(ArxivPaper)
        .exists()
    )
    filters = []
    if status is not None:
        filters.append(ArxivPaper.status == status)
    if date_from is not None:
        filters.append(ArxivPaper.submitted_at >= datetime.combine(date_from, time.min, UTC))
    if date_to is not None:
        filters.append(ArxivPaper.submitted_at <= datetime.combine(date_to, time.max, UTC))
    if has_summary is not None:
        filters.append(summary_ready if has_summary else ~summary_ready)

    page = (
        select(ArxivPaper, summary_ready.label("has_summary"))
        .where(*filters)
        .order_by(*paper_order(ArxivPaper, sort))
        .limit(limit)
        .offset(offset)
        .subquery()
    )
    total = select(func.count().label("total_count")).select_from(ArxivPaper).where(*filters).subquery()
    # Keep the count on empty pages and read it from the same snapshot as the page.
    statement = (
        select(*page.c, total.c.total_count)
        .select_from(total.outerjoin(page, true()))
        .order_by(*paper_order(page.c, sort))
    )
    async with database_session(request) as session:
        rows = (await session.execute(statement)).mappings().all()
        response.headers["X-Total-Count"] = str(rows[0]["total_count"])
        return [row for row in rows if row["arxiv_id"] is not None]


@router.get("/{arxiv_id:path}/chunks", response_model=list[PaperChunkResponse])
async def list_paper_chunks(request: Request, arxiv_id: ArxivId):
    async with database_session(request) as session:
        rows = (await session.execute(
            text("""
                SELECT chunk.id AS chunk_id, link.chunk_index, chunk.text
                FROM arxiv_papers AS paper
                LEFT JOIN current_paper_chunks AS link ON link.arxiv_id = paper.arxiv_id AND link.source_kind = 'article'
                LEFT JOIN chunks AS chunk ON chunk.id = link.chunk_id
                WHERE paper.arxiv_id = :arxiv_id
                ORDER BY link.chunk_index
            """),
            {"arxiv_id": arxiv_id},
        )).mappings().all()
    if not rows:
        raise HTTPException(404, "Paper not found.")
    return [row for row in rows if row["chunk_id"] is not None]


@router.get("/{arxiv_id:path}/text", response_class=StreamingResponse)
async def get_paper_text(request: Request, arxiv_id: ArxivId):
    paper = await get_paper(request, arxiv_id)
    key = paper.text_object_key
    media_type = TEXT_MEDIA_TYPES.get(PurePosixPath(key).suffix.lower()) if key else None
    if media_type is None:
        raise HTTPException(404, "Paper text not found.")
    return await paper_file(request, paper.bucket, key, media_type)


@router.get("/{arxiv_id:path}/html", response_class=StreamingResponse)
async def download_paper_html(request: Request, arxiv_id: ArxivId):
    paper = await get_paper(request, arxiv_id)
    source = PurePosixPath(paper.object_key)
    if source.suffix.lower() != ".html":
        raise HTTPException(404, "Paper HTML not found.")
    return await paper_file(request, paper.bucket, paper.object_key, "application/octet-stream", source.name)


@router.get("/{arxiv_id:path}", response_model=PaperResponse)
async def get_paper(request: Request, arxiv_id: ArxivId):
    async with database_session(request) as session:
        paper = await session.get(ArxivPaper, arxiv_id)
    if paper is None:
        raise HTTPException(404, "Paper not found.")
    return paper
