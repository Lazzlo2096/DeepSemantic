from datetime import UTC, date, datetime, time
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import case, func, select, true
from sqlalchemy.orm import aliased

from database import MAX_BIGINT, ArxivPaper, database_session


router = APIRouter(prefix="/papers", tags=["papers"])
ArxivId = Annotated[str, Path(min_length=1, pattern=r"^[^\x00]+$")]
PaperStatus = Literal["downloaded", "texted", "chunked", "embeddinged"]
PaperSort = Literal["downloaded_desc", "submitted_desc", "submitted_asc", "status"]


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


def paper_order(paper, sort: PaperSort):
    if sort == "submitted_asc":
        return paper.submitted_at.asc(), paper.arxiv_id
    if sort == "submitted_desc":
        return paper.submitted_at.desc(), paper.arxiv_id
    if sort == "status":
        stage = case(
            {"downloaded": 0, "texted": 1, "chunked": 2, "embeddinged": 3},
            value=paper.status,
            else_=4,
        )
        return stage, paper.submitted_at.desc(), paper.arxiv_id
    return paper.downloaded_at.desc(), paper.arxiv_id


@router.get("", response_model=list[PaperResponse])
async def list_papers(
    request: Request,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=MAX_BIGINT)] = 0,
    sort: PaperSort = "downloaded_desc",
    status: PaperStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
):
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to.")

    filters = []
    if status is not None:
        filters.append(ArxivPaper.status == status)
    if date_from is not None:
        filters.append(ArxivPaper.submitted_at >= datetime.combine(date_from, time.min, UTC))
    if date_to is not None:
        filters.append(ArxivPaper.submitted_at <= datetime.combine(date_to, time.max, UTC))

    page = (
        select(ArxivPaper)
        .where(*filters)
        .order_by(*paper_order(ArxivPaper, sort))
        .limit(limit)
        .offset(offset)
        .subquery()
    )
    paper = aliased(ArxivPaper, page)
    total = select(func.count().label("total_count")).select_from(ArxivPaper).where(*filters).subquery()
    # Keep the count on empty pages and read it from the same snapshot as the page.
    statement = (
        select(paper, total.c.total_count)
        .select_from(total.outerjoin(page, true()))
        .order_by(*paper_order(paper, sort))
    )
    async with database_session(request) as session:
        rows = (await session.execute(statement)).all()
        response.headers["X-Total-Count"] = str(rows[0].total_count)
        return [paper for paper, _ in rows if paper is not None]


@router.get("/{arxiv_id:path}", response_model=PaperResponse)
async def get_paper(request: Request, arxiv_id: ArxivId):
    async with database_session(request) as session:
        paper = await session.get(ArxivPaper, arxiv_id)
    if paper is None:
        raise HTTPException(404, "Paper not found.")
    return paper
