import asyncio
import os
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select, text, update

from database import MAX_BIGINT, Chunk, database_session
from embedding_schemas import EMBEDDING_MODEL_KEY, EmbeddingRequest, Text


router = APIRouter(prefix="/chunks", tags=["chunks"])
OPENROUTER_COLLECTION_PREFIXES = {
    "article": "documents_openrouter_",
    "summary": "paper_summaries_openrouter_",
}
PAPER_COLLECTIONS = {
    kind: f"{prefix}{EMBEDDING_MODEL_KEY}" for kind, prefix in OPENROUTER_COLLECTION_PREFIXES.items()
}
PaperChunkSource = Literal["article", "summary"]
ChunkId = Annotated[int, Path(ge=1, le=MAX_BIGINT)]


def require_zero_chunk_overlap() -> None:
    if os.getenv("CHUNK_OVERLAP_PERCENT", "0").strip() != "0":
        raise HTTPException(503, "Search over stored chunks requires CHUNK_OVERLAP_PERCENT=0 (0% overlap).")


class ChunkRequest(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def validate_text(cls, text: str) -> str:
        if not text.strip() or "\x00" in text:
            raise ValueError("Text must contain content and cannot contain NUL characters")
        return text


class ChunkResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


class ChunkDetail(ChunkResponse):
    text: str
    qdrant_collection: str | None
    qdrant_point_id: int | None


class ChunkSearchRequest(BaseModel):
    query: Text
    limit: int = Field(default=10, ge=1, le=50)


class ChunkTextSearchRequest(ChunkSearchRequest):
    query: Annotated[Text, Field(max_length=1000, pattern=r"^[^\x00]*$")]


class ChunkMatch(BaseModel):
    chunk_id: int
    text: str
    arxiv_id: str | None
    paper_title: str | None
    chunk_index: int | None
    source_kind: PaperChunkSource | None = None
    source_start: int | None = None
    source_end: int | None = None
    source_version: str | None = Field(default=None, exclude=True)


class ChunkSearchResult(ChunkMatch):
    score: float


@router.post("/search", response_model=list[ChunkSearchResult])
async def search_chunks(payload: ChunkSearchRequest, request: Request):
    return await find_chunks(request, payload.query, payload.limit)


@router.post("/search/text", response_model=list[ChunkMatch])
async def search_chunk_text(payload: ChunkTextSearchRequest, request: Request):
    return await find_text_chunks(request, payload.query, payload.limit)


async def find_text_chunks(request: Request, query: str, limit: int, *, located_only: bool = False) -> list[ChunkMatch]:
    require_zero_chunk_overlap()
    literal = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    async with database_session(request) as session:
        rows = (await session.execute(
            text("""
                SELECT chunk.id AS chunk_id, chunk.text, paper.arxiv_id,
                       paper.title AS paper_title, link.chunk_index, link.source_kind,
                       link.source_start, link.source_end, link.source_version
                FROM chunks AS chunk
                LEFT JOIN paper_chunks AS link ON link.chunk_id = chunk.id
                LEFT JOIN current_paper_chunks AS current ON current.chunk_id = link.chunk_id
                LEFT JOIN arxiv_papers AS paper ON paper.arxiv_id = link.arxiv_id
                WHERE chunk.text ILIKE :pattern ESCAPE '!'
                  AND (link.chunk_id IS NULL OR current.chunk_id IS NOT NULL)
                  AND (NOT :located_only OR link.source_start IS NOT NULL)
                ORDER BY chunk.id
                LIMIT :limit
            """),
            {"pattern": f"%{literal}%", "limit": limit, "located_only": located_only},
        )).mappings().all()
    return [ChunkMatch(**row) for row in rows]


async def query_collection(qdrant: httpx.AsyncClient, collection: str, embedding, limit: int):
    try:
        response = await qdrant.post(
            f"/collections/{collection}/points/query",
            json={
                "query": embedding["embeddings"][0],
                "limit": limit,
                "filter": {"must": [
                    {"key": "model", "match": {"value": embedding["model"]}},
                    {"key": "model_revision", "match": {"value": embedding["revision"]}},
                ]},
            },
        )
        if response.status_code == 404:
            return {}
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise HTTPException(503, "Could not search Qdrant.") from error

    return {
        point["id"]: point["score"]
        for point in response.json()["result"]["points"]
        if isinstance(point["id"], int) and 1 <= point["id"] <= MAX_BIGINT
    }


async def find_chunks(request: Request, query: str, limit: int, *, located_only: bool = False) -> list[ChunkSearchResult]:
    require_zero_chunk_overlap()
    state = request.app.state
    embedding = await state.embeddings.embed(EmbeddingRequest(texts=[query]))
    collections = tuple(PAPER_COLLECTIONS.values())
    candidate_limit = limit * 3 if located_only else limit
    results = await asyncio.gather(*(
        query_collection(state.qdrant, collection, embedding, candidate_limit) for collection in collections
    ))
    scores = dict(zip(collections, results, strict=True))
    chunk_ids = {chunk_id for collection_scores in results for chunk_id in collection_scores}
    if not chunk_ids:
        return []

    async with database_session(request) as session:
        rows = (await session.execute(
            text("""
                SELECT chunk.id AS chunk_id, chunk.text, chunk.qdrant_collection, paper.arxiv_id,
                       paper.title AS paper_title, link.chunk_index, link.source_kind,
                       link.source_start, link.source_end, link.source_version
                FROM chunks AS chunk
                LEFT JOIN paper_chunks AS link ON link.chunk_id = chunk.id
                LEFT JOIN current_paper_chunks AS current ON current.chunk_id = link.chunk_id
                LEFT JOIN arxiv_papers AS paper ON paper.arxiv_id = link.arxiv_id
                WHERE chunk.id = ANY(:chunk_ids)
                  AND chunk.qdrant_point_id = chunk.id
                  AND (link.chunk_id IS NULL OR current.chunk_id IS NOT NULL)
                  AND (NOT :located_only OR link.source_start IS NOT NULL)
            """),
            {"chunk_ids": list(chunk_ids), "located_only": located_only},
        )).mappings().all()
    matches = []
    for row in rows:
        score = scores.get(row["qdrant_collection"], {}).get(row["chunk_id"])
        if score is not None:
            matches.append(ChunkSearchResult(**row, score=score))
    return sorted(matches, key=lambda match: (-match.score, match.chunk_id))[:limit]


@router.post("", response_model=ChunkResponse, status_code=201)
async def create_chunk(payload: ChunkRequest, request: Request):
    async with database_session(request) as session:
        chunk = Chunk(text=payload.text)
        session.add(chunk)
    return chunk


async def read_chunk(request: Request, chunk_id: int):
    async with database_session(request) as session:
        chunk = await session.get(Chunk, chunk_id)
    if chunk is None:
        raise HTTPException(404, "Chunk not found.")
    return chunk


@router.get("/{chunk_id}", response_model=ChunkDetail)
async def get_chunk(request: Request, chunk_id: ChunkId):
    return await read_chunk(request, chunk_id)


async def upsert_chunk_embeddings(qdrant: httpx.AsyncClient, chunk_ids: list[int], result, collection: str):
    collection_path = f"/collections/{collection}"
    if len(result["embeddings"]) != len(chunk_ids):
        raise HTTPException(502, "Embedding worker returned the wrong number of vectors.")
    points = [
        {
            "id": chunk_id,
            "vector": vector,
            "payload": {
                "document_id": chunk_id,
                "model": result["model"],
                "model_revision": result["revision"],
            },
        }
        for chunk_id, vector in zip(chunk_ids, result["embeddings"], strict=True)
    ]
    try:
        response = await qdrant.put(f"{collection_path}/points?wait=true", json={"points": points})
        if response.status_code == 404:
            created = await qdrant.put(
                collection_path,
                json={"vectors": {"size": result["dimensions"], "distance": "Cosine"}},
            )
            if created.status_code != 409:
                created.raise_for_status()
            response = await qdrant.put(f"{collection_path}/points?wait=true", json={"points": points})
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise HTTPException(503, "Could not save the embedding to Qdrant.") from error
    if response.json().get("result", {}).get("status") != "completed":
        raise HTTPException(503, "Qdrant did not confirm the embedding write.")


async def save_chunk_embedding_references(
    session, chunk_ids: list[int], collection: str,
) -> None:
    updated = await session.scalars(
        update(Chunk)
        .where(Chunk.id.in_(chunk_ids))
        .values(qdrant_collection=collection, qdrant_point_id=Chunk.id)
        .returning(Chunk.id)
    )
    if set(updated) != set(chunk_ids):
        raise HTTPException(404, "Chunk not found.")


@router.get("/{chunk_id}/embedding", response_model=list[float])
async def get_chunk_embedding(request: Request, chunk_id: ChunkId):
    chunk = await read_chunk(request, chunk_id)
    if chunk.qdrant_collection is None or chunk.qdrant_point_id is None:
        raise HTTPException(404, "Chunk embedding not found.")

    collection = quote(chunk.qdrant_collection, safe="")
    try:
        response = await request.app.state.qdrant.get(
            f"/collections/{collection}/points/{chunk.qdrant_point_id}"
        )
        if response.status_code == 404:
            raise HTTPException(404, "Chunk embedding not found.")
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise HTTPException(503, "Could not read the embedding from Qdrant.") from error

    vector = response.json()["result"].get("vector")
    if not isinstance(vector, list):
        raise HTTPException(502, "Qdrant did not return a dense embedding.")
    return vector


@router.post("/{chunk_id}/embedding", response_model=list[float])
async def embed_chunk(request: Request, chunk_id: ChunkId):
    chunk = await read_chunk(request, chunk_id)
    try:
        payload = EmbeddingRequest(texts=[chunk.text])
    except ValidationError as error:
        raise HTTPException(422, "Stored chunk text does not meet embedding input limits.") from error
    result = await request.app.state.embeddings.embed(payload)
    async with database_session(request) as session:
        # Serialize the write with source replacement so it cannot recreate a deleted point.
        if await session.get(Chunk, chunk_id, with_for_update=True) is None:
            raise HTTPException(404, "Chunk not found.")
        source_kind = await session.scalar(text("SELECT source_kind FROM paper_chunks WHERE chunk_id = :id"),
                                           {"id": chunk_id})
        collection = PAPER_COLLECTIONS.get(source_kind, PAPER_COLLECTIONS["article"])
        await upsert_chunk_embeddings(request.app.state.qdrant, [chunk_id], result, collection)
        await save_chunk_embedding_references(session, [chunk_id], collection)
    return result["embeddings"][0]


@router.get("", response_model=list[ChunkDetail])
async def get_chunks(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=MAX_BIGINT)] = 0,
):
    async with database_session(request) as session:
        result = await session.scalars(
            select(Chunk).order_by(Chunk.id).limit(limit).offset(offset)
        )
        return result.all()
