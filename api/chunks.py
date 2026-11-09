from datetime import datetime
from typing import Annotated
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select, text, update

from database import MAX_BIGINT, Chunk, database_session
from embedding_schemas import EmbeddingRequest, Text


router = APIRouter(prefix="/chunks", tags=["chunks"])
QDRANT_COLLECTION = "documents_bge_m3"
ChunkId = Annotated[int, Path(ge=1, le=MAX_BIGINT)]


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


class ChunkSearchResult(BaseModel):
    chunk_id: int
    text: str
    score: float
    arxiv_id: str | None
    paper_title: str | None
    chunk_index: int | None


@router.post("/search", response_model=list[ChunkSearchResult])
async def search_chunks(payload: ChunkSearchRequest, request: Request):
    return await find_chunks(request, payload.query, payload.limit)


async def find_chunks(request: Request, query: str, limit: int) -> list[ChunkSearchResult]:
    embedding = await request.app.state.rpc.embed(EmbeddingRequest(texts=[query]))
    try:
        response = await request.app.state.qdrant.post(
            f"/collections/{QDRANT_COLLECTION}/points/query",
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
            return []
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise HTTPException(503, "Could not search Qdrant.") from error

    scores = {
        point["id"]: point["score"]
        for point in response.json()["result"]["points"]
        if isinstance(point["id"], int) and 1 <= point["id"] <= MAX_BIGINT
    }
    if not scores:
        return []

    async with database_session(request) as session:
        rows = (await session.execute(
            text("""
                SELECT chunk.id AS chunk_id, chunk.text, paper.arxiv_id,
                       paper.title AS paper_title, link.chunk_index
                FROM chunks AS chunk
                LEFT JOIN paper_chunks AS link ON link.chunk_id = chunk.id
                LEFT JOIN arxiv_papers AS paper ON paper.arxiv_id = link.arxiv_id
                WHERE chunk.id = ANY(:chunk_ids)
                  AND chunk.qdrant_collection = :collection
                  AND chunk.qdrant_point_id = chunk.id
            """),
            {"chunk_ids": list(scores), "collection": QDRANT_COLLECTION},
        )).mappings().all()
    chunks_by_id = {row["chunk_id"]: row for row in rows}
    return [
        ChunkSearchResult(**chunks_by_id[chunk_id], score=score)
        for chunk_id, score in scores.items()
        if chunk_id in chunks_by_id
    ]


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


async def upsert_chunk_embeddings(qdrant: httpx.AsyncClient, chunk_ids: list[int], result):
    collection_path = f"/collections/{QDRANT_COLLECTION}"
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
    session, chunk_ids: list[int],
) -> None:
    updated = await session.scalars(
        update(Chunk)
        .where(Chunk.id.in_(chunk_ids))
        .values(qdrant_collection=QDRANT_COLLECTION, qdrant_point_id=Chunk.id)
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
    result = await request.app.state.rpc.embed(payload)
    await upsert_chunk_embeddings(request.app.state.qdrant, [chunk_id], result)
    async with database_session(request) as session:
        await save_chunk_embedding_references(session, [chunk_id])
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
