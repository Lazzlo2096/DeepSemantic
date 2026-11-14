import asyncio
import logging

import httpx
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import delete, insert, text
from tokenizers import Tokenizer

from chunks import (
    OPENROUTER_COLLECTION_PREFIXES,
    PAPER_COLLECTIONS,
    save_chunk_embedding_references,
    upsert_chunk_embeddings,
)
from database import Chunk
from embedding_schemas import EmbeddingRequest
from paper_chunking import split_document


BATCH_SIZE = 8
def read_article(s3, bucket: str, key: str) -> str:
    with s3.get_object(Bucket=bucket, Key=key)["Body"] as body:
        return body.read().decode("utf-8-sig")


async def replace_source_chunks(state, s3, source, tokenizer: Tokenizer, max_tokens: int) -> int:
    params = {"arxiv_id": source.arxiv_id, "source_kind": source.source_kind, "source_version": source.source_version}
    if source.source_kind == "article":
        document = await asyncio.to_thread(read_article, s3, source.bucket, source.text_object_key)
    else:
        async with state.db_sessions.begin() as session:
            document = await session.scalar(text("""
                SELECT summary FROM paper_summaries
                WHERE arxiv_id = :arxiv_id AND status = 'completed' AND md5(summary) = :source_version
            """), params)
        if document is None:
            return 0

    fragments = list(split_document(document, tokenizer, max_tokens))
    if not fragments:
        raise ValueError("Source has no chunk content")

    async with state.db_sessions.begin() as session:
        # Recheck the version after reading the source and lock it until the replacement commits.
        if source.source_kind == "article":
            current = await session.scalar(text("""
                SELECT jsonb_build_array(bucket, text_object_key)::text FROM arxiv_papers
                WHERE arxiv_id = :arxiv_id AND status = 'texted' AND text_object_key IS NOT NULL
                FOR NO KEY UPDATE
            """), params)
        else:
            current = await session.scalar(text("""
                SELECT md5(summary) FROM paper_summaries
                WHERE arxiv_id = :arxiv_id AND status = 'completed'
                FOR NO KEY UPDATE
            """), params)
        if current != source.source_version:
            return 0

        existing = (await session.execute(text("""
            SELECT chunk.id, chunk.text, chunk.qdrant_collection, link.chunk_index, link.source_version
            FROM chunks AS chunk JOIN paper_chunks AS link ON link.chunk_id = chunk.id
            WHERE link.arxiv_id = :arxiv_id AND link.source_kind = :source_kind
            ORDER BY link.chunk_index
            FOR UPDATE OF chunk
        """), params)).all()
        if len(existing) == len(fragments) and all(
            row.chunk_index == index and row.source_version == current and row.text == fragment.text
            for index, (row, fragment) in enumerate(zip(existing, fragments, strict=True))
        ):
            await session.execute(text("""
                UPDATE paper_chunks SET source_start = :source_start, source_end = :source_end
                WHERE chunk_id = :chunk_id
            """), [
                {"chunk_id": row.id, "source_start": fragment.source_start, "source_end": fragment.source_end}
                for row, fragment in zip(existing, fragments, strict=True)
            ])
            return len(fragments)
        if existing:
            old_ids = [row.id for row in existing]
            # Include points whose writes succeeded before their PostgreSQL references were committed.
            catalog = await state.qdrant.get("/collections")
            catalog.raise_for_status()
            prefix = OPENROUTER_COLLECTION_PREFIXES[source.source_kind]
            collections = {PAPER_COLLECTIONS[source.source_kind]}
            collections.update(row.qdrant_collection for row in existing if row.qdrant_collection)
            collections.update(
                entry["name"] for entry in catalog.json()["result"]["collections"]
                if entry["name"].startswith(prefix)
            )
            for collection in sorted(collections):
                response = await state.qdrant.post(
                    f"/collections/{collection}/points/delete?wait=true", json={"points": old_ids},
                )
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                if response.json().get("result", {}).get("status") != "completed":
                    raise HTTPException(503, "Qdrant did not confirm the chunk deletion.")
            await session.execute(text("""
                DELETE FROM paper_chunks WHERE arxiv_id = :arxiv_id AND source_kind = :source_kind
            """), params)
            await session.execute(delete(Chunk).where(Chunk.id.in_(old_ids)))

        ids = await session.scalars(
            insert(Chunk).returning(Chunk.id, sort_by_parameter_order=True),
            [{"text": fragment.text} for fragment in fragments],
        )
        await session.execute(text("""
            INSERT INTO paper_chunks (chunk_id, arxiv_id, source_kind, source_version, chunk_index, source_start, source_end)
            VALUES (:chunk_id, :arxiv_id, :source_kind, :source_version, :chunk_index, :source_start, :source_end)
        """), [
            {
                **params, "chunk_id": chunk_id, "chunk_index": index,
                "source_start": fragment.source_start, "source_end": fragment.source_end,
            }
            for index, (chunk_id, fragment) in enumerate(zip(ids, fragments, strict=True))
        ])
    return len(fragments)


async def index_batch(state, batch, collection: str) -> tuple[int, int]:
    chunk_ids = [chunk.id for chunk in batch]
    try:
        payload = EmbeddingRequest(texts=[chunk.text for chunk in batch])
        result = await state.embeddings.embed(payload)
    except (HTTPException, ValidationError) as error:
        if isinstance(error, HTTPException) and error.status_code != 422:
            raise
        if len(batch) == 1:
            logging.error("Chunk %s could not be embedded: %s", chunk_ids[0], error)
            return 0, 1

        # A single rejected text must not prevent the other chunks from being indexed.
        midpoint = len(batch) // 2
        left_saved, left_failed = await index_batch(state, batch[:midpoint], collection)
        right_saved, right_failed = await index_batch(state, batch[midpoint:], collection)
        return left_saved + right_saved, left_failed + right_failed

    await upsert_chunk_embeddings(state.qdrant, chunk_ids, result, collection)
    async with state.db_sessions.begin() as session:
        await save_chunk_embedding_references(session, chunk_ids, collection)
    return len(chunk_ids), 0


if __name__ == "__main__":
    from processing.worker import run
    asyncio.run(run("index"))
