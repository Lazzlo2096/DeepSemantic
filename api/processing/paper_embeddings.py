import logging
from types import SimpleNamespace

import httpx
from fastapi import HTTPException
from pydantic import ValidationError

import paper_repository as repository
from chunks import save_chunk_embedding_references, upsert_chunk_embeddings
from embedding_schemas import EmbeddingRequest
from paper_chunking import split_document

BATCH_SIZE = 8


async def prepare_source(state, source, document, tokenizer, max_tokens, generation):
    fragments = list(split_document(document, tokenizer, max_tokens))
    if not fragments:
        raise ValueError("Source has no chunk content")
    return await repository.replace_chunks(state, SimpleNamespace(**source), fragments, generation)


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

