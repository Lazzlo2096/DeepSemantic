import argparse
import asyncio
import logging

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text

from chunks import save_chunk_embedding_references, upsert_chunk_embeddings
from embedding_schemas import EmbeddingRequest
from main import app, lifespan


BATCH_SIZE = 8
POLL_INTERVAL_SECONDS = 15
INDEX_LOCK = text("SELECT pg_try_advisory_lock(hashtext('deepsemantic.paper-embeddings'))")
INDEX_UNLOCK = text("SELECT pg_advisory_unlock(hashtext('deepsemantic.paper-embeddings'))")
PENDING_CHUNKS = text("""
    SELECT chunk.id, chunk.text
    FROM chunks AS chunk
    JOIN paper_chunks AS link ON link.chunk_id = chunk.id
    JOIN arxiv_papers AS paper ON paper.arxiv_id = link.arxiv_id
    WHERE paper.status = 'chunked'
      AND (chunk.qdrant_collection IS NULL OR chunk.qdrant_point_id IS NULL)
      AND chunk.id > :after_id AND chunk.id <= :last_id
    ORDER BY chunk.id
    LIMIT :batch_size
""")


async def index_batch(state, batch) -> tuple[int, int]:
    chunk_ids = [chunk.id for chunk in batch]
    try:
        payload = EmbeddingRequest(texts=[chunk.text for chunk in batch])
        result = await state.rpc.embed(payload)
    except (HTTPException, ValidationError) as error:
        if isinstance(error, HTTPException) and error.status_code != 422:
            raise
        if len(batch) == 1:
            logging.error("Chunk %s could not be embedded: %s", chunk_ids[0], error)
            return 0, 1

        # A single rejected text must not prevent the other chunks from being indexed.
        midpoint = len(batch) // 2
        left_saved, left_failed = await index_batch(state, batch[:midpoint])
        right_saved, right_failed = await index_batch(state, batch[midpoint:])
        return left_saved + right_saved, left_failed + right_failed

    await upsert_chunk_embeddings(state.qdrant, chunk_ids, result)
    async with state.db_sessions.begin() as session:
        await save_chunk_embedding_references(session, chunk_ids)
    return len(chunk_ids), 0


async def index_chunked_papers(state) -> int:
    async with state.db_engine.connect() as lock_connection:
        locked = await lock_connection.scalar(INDEX_LOCK)
        await lock_connection.commit()
        if not locked:
            raise RuntimeError("Paper indexing is already running.")
        try:
            async with state.db_sessions.begin() as session:
                last_id = await session.scalar(text("SELECT coalesce(max(id), 0) FROM chunks"))

            after_id = saved = failed = 0
            while after_id < last_id:
                async with state.db_sessions.begin() as session:
                    batch = (await session.execute(
                        PENDING_CHUNKS,
                        {"after_id": after_id, "last_id": last_id, "batch_size": BATCH_SIZE},
                    )).all()
                if not batch:
                    break

                batch_saved, batch_failed = await index_batch(state, batch)
                saved += batch_saved
                failed += batch_failed
                after_id = batch[-1].id
                logging.info("Indexed %d chunks, %d failed (through chunk %d)", saved, failed, after_id)

            logging.info("Finished: %d chunks indexed, %d failed", saved, failed)
            return int(failed > 0)
        finally:
            await lock_connection.execute(INDEX_UNLOCK)


async def main(watch: bool = False) -> int:
    async with lifespan(app):
        if not watch:
            return await index_chunked_papers(app.state)

        while True:
            try:
                await index_chunked_papers(app.state)
            except (HTTPException, RuntimeError) as error:
                logging.warning("Paper indexing will retry in %d seconds: %s", POLL_INTERVAL_SECONDS, error)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Index chunks of arXiv papers.")
    parser.add_argument("--watch", action="store_true", help="Keep checking for newly chunked papers")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        raise SystemExit(asyncio.run(main(args.watch)))
    except (HTTPException, RuntimeError) as error:
        logging.error("Paper indexing stopped: %s", error)
        raise SystemExit(1) from error
