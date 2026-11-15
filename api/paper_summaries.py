from datetime import datetime
from uuid import UUID, NAMESPACE_URL, uuid5

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from database import PaperSummaryStatus
from papers import ArxivId
import paper_repository as repository
from processing.config import pipeline_settings, summary_generation, summary_workflow_id
import temporal_runtime

MARKDOWN_REQUIRED = "Для суммаризации нужен полный текст в Markdown. Импортируйте статью в режиме HTML."
router = APIRouter(prefix="/papers", tags=["papers"])


class SummaryJob(BaseModel):
    id: UUID
    arxiv_id: str
    workflow_id: str | None = None
    status: PaperSummaryStatus
    parts_completed: int
    parts_total: int
    model: str | None
    prompt_version: str | None
    summary: str | None
    last_error: str | None
    finished_at: datetime | None


async def summary_response(request, arxiv_id, response=None, create=False):
    paper = await repository.get_paper(arxiv_id)
    if paper is None:
        raise HTTPException(404, "Paper not found.")
    cached = await repository.get_summary(arxiv_id)
    if cached:
        if response is not None:
            response.status_code = 200
        return {**cached, "status": "completed", "parts_completed": 1, "parts_total": 1,
                "last_error": None, "finished_at": cached["generated_at"]}
    if not paper["text_object_key"] or not paper["text_object_key"].lower().endswith('.md'):
        if create:
            raise HTTPException(409, MARKDOWN_REQUIRED)
        return None
    settings = pipeline_settings()
    identity = summary_workflow_id(paper, settings)
    deferred = (await repository.get_pipeline_settings())["summary_mode"] == "deferred"
    if create and not deferred:
        await temporal_runtime.start(request, 'SummaryWorkflow', {'paper': {
            key: paper[key] for key in ('arxiv_id', 'bucket', 'object_key', 'text_object_key', 'source_version', 'markdown_version')
        }, 'settings': settings}, identity, allow_completed=True)
    runtime = None if deferred else await temporal_runtime.describe(request, identity)
    if runtime is None and not deferred:
        return None
    if deferred:
        runtime = {'status': 'deferred', 'progress': {}, 'last_error': None, 'finished_at': None}
    status = runtime['status']
    if status == 'completed':
        cached = await repository.get_summary(arxiv_id)
        if cached:
            return {**cached, 'status': 'completed', 'parts_completed': 1, 'parts_total': 1,
                    'last_error': None, 'finished_at': cached['generated_at']}
        status = 'failed'
    return {'id': uuid5(NAMESPACE_URL, f"{arxiv_id}:{summary_generation(paper, settings)}"),
            'arxiv_id': arxiv_id, 'workflow_id': identity, 'status': status,
            'parts_completed': runtime['progress'].get('parts_completed', 0), 'parts_total': 1,
            'model': settings['summary']['model'], 'prompt_version': settings['summary']['prompt_version'],
            'summary': None, 'last_error': runtime['last_error'], 'finished_at': runtime['finished_at']}


@router.get('/{arxiv_id:path}/summary', response_model=SummaryJob | None)
async def get_paper_summary(request: Request, arxiv_id: ArxivId):
    return await summary_response(request, arxiv_id)


@router.post('/{arxiv_id:path}/summary', status_code=202, response_model=SummaryJob, responses={200: {'model': SummaryJob}})
async def create_paper_summary(request: Request, response: Response, arxiv_id: ArxivId):
    return await summary_response(request, arxiv_id, response, create=True)
