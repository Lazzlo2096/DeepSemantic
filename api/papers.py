from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from database import MAX_BIGINT, ArxivPaper, database_session


router = APIRouter(prefix="/papers", tags=["papers"])
ArxivId = Annotated[str, Path(min_length=1, pattern=r"^[^\x00]+$")]


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


@router.get("", response_model=list[PaperResponse])
async def list_papers(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=MAX_BIGINT)] = 0,
):
    async with database_session(request) as session:
        papers = await session.scalars(
            select(ArxivPaper)
            .order_by(ArxivPaper.downloaded_at.desc(), ArxivPaper.arxiv_id)
            .limit(limit)
            .offset(offset)
        )
        return papers.all()


@router.get("/{arxiv_id:path}", response_model=PaperResponse)
async def get_paper(request: Request, arxiv_id: ArxivId):
    async with database_session(request) as session:
        paper = await session.get(ArxivPaper, arxiv_id)
    if paper is None:
        raise HTTPException(404, "Paper not found.")
    return paper
