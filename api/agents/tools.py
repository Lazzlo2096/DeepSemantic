"""Source-search and document-reading tools reusable across agents.

Call create_tools(request, versions) with a version dictionary scoped to a single
run. Tools share this dictionary to avoid mixing different source versions.
"""

from codecs import getincrementaldecoder
from functools import partial
from typing import Annotated

from botocore.exceptions import BotoCoreError
from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import text

from chunks import PaperChunkSource, find_chunks, find_text_chunks
from database import database_session
from papers import open_paper_object


QueryText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, pattern=r"^[^\x00]+$")]


class SearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: QueryText
    limit: int = Field(default=3, ge=1)


class ReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    document_id: Annotated[str, StringConstraints(min_length=1, pattern=r"^[^\x00]+$")]
    source_kind: PaperChunkSource = "article"
    source_start: int = Field(ge=0, description="Индекс первого символа Unicode, начиная с нуля.")
    source_end: int = Field(gt=0, description="Индекс сразу после последнего символа Unicode.")

    @model_validator(mode="after")
    def validate_range(self):
        if self.source_end <= self.source_start:
            raise ValueError("source_end must be greater than source_start")
        return self


def read_utf8_slice(body, start: int, end: int) -> str:
    decoder = getincrementaldecoder("utf-8-sig")()
    position = 0
    parts = []
    with body:
        for data in body.iter_chunks(chunk_size=16 * 1024):
            decoded = decoder.decode(data)
            parts.append(decoded[max(0, start - position):end - position])
            position += len(decoded)
            if position >= end:
                return "".join(parts)
        decoder.decode(b"", final=True)
    raise HTTPException(422, f"Range exceeds the source length of {position} characters.")


async def read_document(request: Request, args: ReadArguments, versions: dict) -> dict:
    async with database_session(request) as session:
        source = (await session.execute(text("""
            SELECT source.source_version, source.bucket, source.text_object_key, paper.title, summary.summary
            FROM paper_chunk_sources AS source
            JOIN arxiv_papers AS paper USING (arxiv_id)
            LEFT JOIN paper_summaries AS summary
                ON summary.arxiv_id = source.arxiv_id AND source.source_kind = 'summary'
            WHERE source.arxiv_id = :document_id AND source.source_kind = :source_kind
        """), {"document_id": args.document_id, "source_kind": args.source_kind})).one_or_none()
    if source is None:
        raise HTTPException(404, "Document text not found.")
    key = (args.document_id, args.source_kind)
    if versions.setdefault(key, source.source_version) != source.source_version:
        raise HTTPException(409, "Source changed during this request.")
    if args.source_kind == "summary":
        if args.source_end > len(source.summary):
            raise HTTPException(422, f"Range exceeds the source length of {len(source.summary)} characters.")
        excerpt = source.summary[args.source_start:args.source_end]
    else:
        stored = await open_paper_object(request, source.bucket, source.text_object_key)
        try:
            excerpt = await run_in_threadpool(read_utf8_slice, stored["Body"], args.source_start, args.source_end)
        except (BotoCoreError, OSError, UnicodeError) as error:
            raise HTTPException(503, "Could not read document text.") from error
    return {**args.model_dump(), "text": excerpt, "paper_title": source.title, "chunk_index": None}


async def search_sources(request: Request, query: str, limit: int, versions: dict, *, vector: bool) -> list[dict]:
    search = find_chunks if vector else find_text_chunks
    matches = await search(request, query, limit, located_only=True)
    results = []
    for chunk in matches:
        key = (chunk.arxiv_id, chunk.source_kind)
        if versions.setdefault(key, chunk.source_version) != chunk.source_version:
            continue
        result = chunk.model_dump(exclude={"arxiv_id", "chunk_id"})
        results.append({"document_id": chunk.arxiv_id, **result})
    return results


async def search_vectors(query: str, limit: int = 3, *, request: Request, versions: dict):
    return await search_sources(request, query, limit, versions, vector=True)


async def search_text(query: str, limit: int = 3, *, request: Request, versions: dict):
    return await search_sources(request, query, limit, versions, vector=False)


async def read_source(
    document_id: str, source_start: int, source_end: int, source_kind: PaperChunkSource = "article",
    *, request: Request, versions: dict,
):
    args = ReadArguments(
        document_id=document_id, source_start=source_start, source_end=source_end, source_kind=source_kind,
    )
    return [await read_document(request, args, versions)]


TOOL_SPECS = {
    "search_vectors": (
        search_vectors, SearchArguments,
        "Смысловой поиск. Возвращает текст чанка, векторное сходство и его координаты.",
    ),
    "search_text": (
        search_text, SearchArguments,
        "Буквальный поиск строки без учёта регистра. Все тексты в индексе сейчас на английском языке. "
        "Формулируй поисковую строку на английском. Возвращает текст чанка и координаты.",
    ),
    "read_document": (
        read_source, ReadArguments,
        "Читает указанный диапазон исходного текста статьи или суммаризации.",
    ),
}
TOOLS = [
    {"type": "function", "function": {"name": name, "description": description, "parameters": arguments.model_json_schema()}}
    for name, (_, arguments, description) in TOOL_SPECS.items()
]


def create_tools(request: Request, versions: dict) -> dict[str, StructuredTool]:
    return {
        name: StructuredTool.from_function(
            coroutine=partial(function, request=request, versions=versions),
            name=name, description=description, args_schema=arguments,
        )
        for name, (function, arguments, description) in TOOL_SPECS.items()
    }
