"""Source-search and document-reading tools reusable across agents.

Call create_tools(request, versions) with a version dictionary scoped to a single
run. Tools share this dictionary to avoid mixing different source versions.
"""

from codecs import getincrementaldecoder
from functools import partial
from typing import Annotated
from uuid import UUID

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


class DocumentListArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    limit: int = Field(default=100, ge=1, le=100)


class FilenameSearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    limit: int = Field(default=20, ge=1, le=100)


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


async def document_rows(
    request: Request, *, filename_query: str | None = None, limit: int = 100,
    space_id: UUID | None = None, public_only: bool = False,
) -> list[dict]:
    pattern = None
    if filename_query is not None:
        literal = filename_query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        pattern = f"%{literal}%"
    async with database_session(request) as session:
        rows = (await session.execute(text("""
            SELECT file.id AS space_file_id, file.space_id, file.filename, file.content_type,
                   file.size_bytes, file.created_at, file.bucket, file.object_key,
                   paper.arxiv_id, COALESCE(paper.title, file.filename) AS title,
                   count(*) OVER() AS total_count
            FROM public.space_files AS file
            JOIN public.spaces AS space ON space.id = file.space_id
            LEFT JOIN external_integration_arxiv.arxiv_papers AS paper
                ON paper.space_file_id = file.id
            WHERE (CAST(:space_id AS uuid) IS NULL OR file.space_id = CAST(:space_id AS uuid))
              AND (NOT :public_only OR space.visibility = 'public')
              AND (CAST(:filename_query AS text) IS NULL
                   OR file.filename ILIKE CAST(:filename_query AS text) ESCAPE '!')
            ORDER BY file.created_at DESC, file.id DESC
            LIMIT :limit
        """), {
            "space_id": str(space_id) if space_id else None,
            "public_only": public_only,
            "filename_query": pattern,
            "limit": limit,
        })).mappings().all()
    documents = []
    for row in rows:
        documents.append({
            "document_id": row["arxiv_id"] or str(row["space_file_id"]),
            "arxiv_id": row["arxiv_id"],
            "space_file_id": row["space_file_id"],
            "space_id": row["space_id"],
            "filename": row["filename"],
            "title": row["title"],
            "paper_title": row["title"],
            "content_type": row["content_type"],
            "size_bytes": row["size_bytes"],
            "created_at": row["created_at"],
            "bucket": row["bucket"],
            "object_key": row["object_key"],
            "total_count": row["total_count"],
            "source_kind": "article",
            "source_start": 0,
            "source_end": 0,
            "chunk_index": None,
            "text": f"Файл: {row['filename']}. Название: {row['title']}.",
        })
    return documents


async def list_documents(limit: int = 100, *, request: Request, space_id: UUID | None = None,
                         public_only: bool = False):
    return await document_rows(request, limit=limit, space_id=space_id, public_only=public_only)


async def search_documents_by_filename(query: str, limit: int = 20, *, request: Request,
                                       space_id: UUID | None = None, public_only: bool = False):
    return await document_rows(
        request, filename_query=query, limit=limit, space_id=space_id, public_only=public_only,
    )


async def read_document(
    request: Request, args: ReadArguments, versions: dict, *, space_id: UUID | None = None,
    public_only: bool = False,
) -> dict:
    async with database_session(request) as session:
        source = (await session.execute(text("""
            SELECT source.source_version, source.bucket, source.text_object_key, paper.title, summary.summary
            FROM paper_chunk_sources AS source
            JOIN arxiv_papers AS paper USING (arxiv_id)
            JOIN public.space_files AS file ON file.id = paper.space_file_id
            JOIN public.spaces AS space ON space.id = file.space_id
            LEFT JOIN paper_summaries AS summary
                ON summary.arxiv_id = source.arxiv_id AND source.source_kind = 'summary'
            WHERE source.arxiv_id = :document_id AND source.source_kind = :source_kind
              AND (CAST(:space_id AS uuid) IS NULL OR file.space_id = CAST(:space_id AS uuid))
              AND (NOT :public_only OR space.visibility = 'public')
        """), {"document_id": args.document_id, "source_kind": args.source_kind,
                "space_id": str(space_id) if space_id else None, "public_only": public_only})).one_or_none()
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


async def search_sources(
    request: Request, query: str, limit: int, versions: dict, *, vector: bool,
    space_id: UUID | None = None, public_only: bool = False,
) -> list[dict]:
    search = find_chunks if vector else find_text_chunks
    matches = await search(request, query, limit, located_only=True, space_id=space_id, public_only=public_only)
    results = []
    for chunk in matches:
        key = (chunk.arxiv_id or chunk.space_file_id, chunk.source_kind)
        if versions.setdefault(key, chunk.source_version) != chunk.source_version:
            continue
        result = chunk.model_dump(exclude={"chunk_id"})
        results.append({"document_id": chunk.arxiv_id or str(chunk.space_file_id), **result})
    return results


async def search_vectors(query: str, limit: int = 3, *, request: Request, versions: dict,
                         space_id: UUID | None = None, public_only: bool = False):
    return await search_sources(request, query, limit, versions, vector=True, space_id=space_id, public_only=public_only)


async def search_text(query: str, limit: int = 3, *, request: Request, versions: dict,
                      space_id: UUID | None = None, public_only: bool = False):
    return await search_sources(request, query, limit, versions, vector=False, space_id=space_id, public_only=public_only)


async def read_source(
    document_id: str, source_start: int, source_end: int, source_kind: PaperChunkSource = "article",
    *, request: Request, versions: dict, space_id: UUID | None = None, public_only: bool = False,
):
    args = ReadArguments(
        document_id=document_id, source_start=source_start, source_end=source_end, source_kind=source_kind,
    )
    return [await read_document(request, args, versions, space_id=space_id, public_only=public_only)]


TOOL_SPECS = {
    "list_documents": (
        list_documents, DocumentListArguments,
        "Возвращает точный список документов в текущем разрешённом Space с именами файлов и метаданными. "
        "Используй для подсчёта и перечисления документов; это не поиск по содержимому.",
    ),
    "search_documents_by_filename": (
        search_documents_by_filename, FilenameSearchArguments,
        "Ищет документы по имени файла в текущем разрешённом Space. Используй, когда пользователь называет файл.",
    ),
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


def create_tools(request: Request, versions: dict, *, space_id: UUID | None = None, public_only: bool = False) -> dict[str, StructuredTool]:
    return {
        name: StructuredTool.from_function(
            coroutine=partial(function, request=request, versions=versions, space_id=space_id, public_only=public_only),
            name=name, description=description, args_schema=arguments,
        )
        for name, (function, arguments, description) in TOOL_SPECS.items()
    }
