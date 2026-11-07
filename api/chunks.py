from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeout

from database import Chunk
from embedding_schemas import EmbeddingRequest


router = APIRouter(prefix="/chunks", tags=["chunks"])
QDRANT_COLLECTION = "documents_bge_m3"
MAX_BIGINT = 2**63 - 1
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


@asynccontextmanager
async def chunk_session(request: Request):
    try:
        async with request.app.state.chunk_sessions.begin() as session:
            yield session
    except (OperationalError, PoolTimeout) as error:
        raise HTTPException(503, "PostgreSQL is unavailable.") from error


@router.post("", response_model=ChunkResponse, status_code=201)
async def create_chunk(payload: ChunkRequest, request: Request):
    async with chunk_session(request) as session:
        chunk = Chunk(text=payload.text)
        session.add(chunk)
    return chunk


async def read_chunk(request: Request, chunk_id: int):
    async with chunk_session(request) as session:
        chunk = await session.get(Chunk, chunk_id)
    if chunk is None:
        raise HTTPException(404, "Chunk not found.")
    return chunk


@router.get("/{chunk_id}", response_model=ChunkDetail)
async def get_chunk(request: Request, chunk_id: ChunkId):
    return await read_chunk(request, chunk_id)


async def upsert_chunk_embedding(qdrant: httpx.AsyncClient, chunk_id: int, result):
    collection_path = f"/collections/{QDRANT_COLLECTION}"
    point = {
        "id": chunk_id,
        "vector": result["embeddings"][0],
        "payload": {
            "document_id": chunk_id,
            "model": result["model"],
            "model_revision": result["revision"],
        },
    }
    try:
        response = await qdrant.put(f"{collection_path}/points?wait=true", json={"points": [point]})
        if response.status_code == 404:
            created = await qdrant.put(
                collection_path,
                json={"vectors": {"size": result["dimensions"], "distance": "Cosine"}},
            )
            if created.status_code != 409:
                created.raise_for_status()
            response = await qdrant.put(f"{collection_path}/points?wait=true", json={"points": [point]})
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise HTTPException(503, "Could not save the embedding to Qdrant.") from error
    if response.json().get("result", {}).get("status") != "completed":
        raise HTTPException(503, "Qdrant did not confirm the embedding write.")


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
    await upsert_chunk_embedding(request.app.state.qdrant, chunk_id, result)
    async with chunk_session(request) as session:
        updated_id = await session.scalar(
            update(Chunk)
            .where(Chunk.id == chunk_id)
            .values(qdrant_collection=QDRANT_COLLECTION, qdrant_point_id=chunk_id)
            .returning(Chunk.id)
        )
        if updated_id is None:
            raise HTTPException(404, "Chunk not found.")
    return result["embeddings"][0]


@router.get("", response_model=list[ChunkDetail])
async def get_chunks(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=MAX_BIGINT)] = 0,
):
    async with chunk_session(request) as session:
        result = await session.scalars(
            select(Chunk).order_by(Chunk.id).limit(limit).offset(offset)
        )
        return result.all()
