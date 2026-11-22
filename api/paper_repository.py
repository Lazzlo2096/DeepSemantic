"""Persistence of source files, completed summaries and derived chunks.

No job statuses, retries, leases or execution progress are stored here.
"""
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

import psycopg
from psycopg.rows import dict_row
from sqlalchemy import delete, insert, text
from fastapi import HTTPException

from database import Chunk
from chunks import OPENROUTER_COLLECTION_PREFIXES, PAPER_COLLECTIONS
from processing.config import fingerprint


async def get_pipeline_settings():
    async with connection() as db:
        return await (await db.execute(
            "SELECT article_mode, summary_mode, updated_at FROM pipeline_settings WHERE singleton"
        )).fetchone()


async def index_progress(source, settings):
    generation = fingerprint([source["source_version"], settings["index"]])
    async with connection() as db:
        row = await (await db.execute("""
            SELECT count(*) AS chunks_total,
                count(*) FILTER (WHERE chunk.qdrant_collection = %s AND chunk.qdrant_point_id = chunk.id) AS chunks_indexed
            FROM current_paper_chunks link JOIN chunks chunk ON chunk.id = link.chunk_id
            WHERE link.arxiv_id = %s AND link.source_kind = %s
              AND link.source_version = %s AND link.generation = %s
        """, (PAPER_COLLECTIONS[source["source_kind"]], source["arxiv_id"], source["source_kind"],
              source["source_version"], generation))).fetchone()
    return {**row, "status": "completed" if row["chunks_total"] and row["chunks_total"] == row["chunks_indexed"] else "pending"}


async def set_import_workflow(import_id, workflow_id):
    async with connection() as db:
        await db.execute('UPDATE arxiv_imports SET workflow_id = %s WHERE id = %s', (workflow_id, import_id))


async def get_paper(arxiv_id):
    async with connection() as db:
        return await (await db.execute("""
            SELECT paper.*, jsonb_build_array(bucket, object_key, downloaded_at)::text AS source_version,
                jsonb_build_array(bucket, text_object_key)::text AS markdown_version
            FROM arxiv_papers paper WHERE arxiv_id = %s
        """, (arxiv_id,))).fetchone()


async def save_paper(article, bucket, object_key, downloaded_at, import_id=None):
    async with connection() as db:
        await db.execute("""
            INSERT INTO arxiv_papers AS paper
                (arxiv_id, bucket, object_key, title, authors, abstract, submitted_at, downloaded_at, arxiv_import_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (arxiv_id) DO UPDATE SET bucket = EXCLUDED.bucket, object_key = EXCLUDED.object_key,
                title = EXCLUDED.title, authors = EXCLUDED.authors, abstract = EXCLUDED.abstract,
                submitted_at = EXCLUDED.submitted_at, downloaded_at = EXCLUDED.downloaded_at,
                arxiv_import_id = EXCLUDED.arxiv_import_id, text_object_key = NULL
            WHERE (paper.bucket, paper.object_key, paper.downloaded_at) IS DISTINCT FROM
                  (EXCLUDED.bucket, EXCLUDED.object_key, EXCLUDED.downloaded_at)
              AND (paper.bucket = EXCLUDED.bucket OR EXCLUDED.downloaded_at > paper.downloaded_at
                   OR (EXCLUDED.downloaded_at = paper.downloaded_at AND EXCLUDED.bucket < paper.bucket))
        """, (article.arxiv_id, bucket, object_key, article.title, article.authors, article.abstract,
              article.submitted_at, downloaded_at, import_id))
    return await get_paper(article.arxiv_id)


async def save_markdown(paper, key):
    async with connection() as db:
        row = await db.execute("""
            UPDATE arxiv_papers SET text_object_key = %s WHERE arxiv_id = %s
              AND jsonb_build_array(bucket, object_key, downloaded_at)::text = %s
        """, (key, paper["arxiv_id"], paper["source_version"]))
        return bool(row.rowcount)


async def get_summary(arxiv_id, *, generation=None):
    async with connection() as db:
        return await (await db.execute("""
            SELECT result.* FROM paper_summaries result JOIN arxiv_papers paper USING (arxiv_id)
            WHERE result.arxiv_id = %s
              AND result.source_version = jsonb_build_array(paper.bucket, paper.text_object_key)::text
              AND (%s::text IS NULL OR result.generation = %s)
        """, (arxiv_id, generation, generation))).fetchone()


async def save_summary(paper, result, summary_id, generation, settings):
    async with connection() as db, db.transaction():
        current = await (await db.execute("""
            SELECT jsonb_build_array(bucket, text_object_key)::text AS version FROM arxiv_papers
            WHERE arxiv_id = %s FOR NO KEY UPDATE
        """, (paper["arxiv_id"],))).fetchone()
        if current is None or current["version"] != paper["markdown_version"]:
            return False
        await db.execute("""
            INSERT INTO paper_summaries (arxiv_id, id, source_version, generation, model, prompt_version, summary, generated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (arxiv_id) DO UPDATE SET id = EXCLUDED.id, source_version = EXCLUDED.source_version,
                generation = EXCLUDED.generation, model = EXCLUDED.model, prompt_version = EXCLUDED.prompt_version,
                summary = EXCLUDED.summary, generated_at = EXCLUDED.generated_at
        """, (paper["arxiv_id"], summary_id, paper["markdown_version"], generation,
              settings["model"], settings["prompt_version"], result))
        return True


async def get_source(arxiv_id, source_kind, expected_version=None):
    async with connection() as db:
        return await (await db.execute("""
            SELECT source.*, result.summary FROM paper_chunk_sources source
            LEFT JOIN paper_summaries result ON result.arxiv_id = source.arxiv_id AND source.source_kind = 'summary'
            WHERE source.arxiv_id = %s AND source.source_kind = %s
              AND (%s::text IS NULL OR source.source_version = %s)
        """, (arxiv_id, source_kind, expected_version, expected_version))).fetchone()


async def chunk_batch(state, source, generation, cursor, collection, force=False):
    async with state.db_sessions.begin() as session:
        return (await session.execute(text("""
            SELECT chunk.id, chunk.text FROM chunks chunk JOIN current_paper_chunks link ON link.chunk_id = chunk.id
            WHERE link.arxiv_id = :arxiv_id AND link.source_kind = :source_kind
              AND link.source_version = :source_version AND link.generation = :generation
              AND chunk.id > :cursor
              AND (:force OR chunk.qdrant_collection IS DISTINCT FROM :collection
                   OR chunk.qdrant_point_id IS DISTINCT FROM chunk.id)
            ORDER BY chunk.id LIMIT 8
        """), {**source, "generation": generation, "cursor": cursor, "collection": collection, "force": force})).all()


async def catalog_page(after="", limit=50):
    async with connection() as db:
        return await (await db.execute("SELECT arxiv_id FROM arxiv_papers WHERE arxiv_id > %s ORDER BY arxiv_id LIMIT %s",
                                      (after, limit))).fetchall()


@asynccontextmanager
async def connection():
    async with await psycopg.AsyncConnection.connect(
        host=os.getenv("POSTGRES_HOST", "postgres"), dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"],
        connect_timeout=5, autocommit=True, row_factory=dict_row,
    ) as db:
        yield db


async def replace_chunks(state, source, fragments, generation):
    params = {"arxiv_id": source.arxiv_id, "source_kind": source.source_kind, "source_version": source.source_version}
    async with state.db_sessions.begin() as session:
        # Recheck the version after reading the source and lock it until the replacement commits.
        if source.source_kind == "article":
            current = await session.scalar(text("""
                SELECT jsonb_build_array(bucket, text_object_key)::text FROM arxiv_papers
                WHERE arxiv_id = :arxiv_id AND text_object_key IS NOT NULL
                FOR NO KEY UPDATE
            """), params)
        else:
            current = await session.scalar(text("""
                SELECT md5(summary) FROM paper_summaries
                WHERE arxiv_id = :arxiv_id
                FOR NO KEY UPDATE
            """), params)
        if current != source.source_version:
            return 0

        existing = (await session.execute(text("""
            SELECT chunk.id, chunk.text, chunk.qdrant_collection, link.chunk_index, link.source_version, link.generation
            FROM chunks AS chunk JOIN paper_chunks AS link ON link.chunk_id = chunk.id
            WHERE link.arxiv_id = :arxiv_id AND link.source_kind = :source_kind
            ORDER BY link.chunk_index
            FOR UPDATE OF chunk
        """), params)).all()
        if len(existing) == len(fragments) and all(
            row.chunk_index == index and row.source_version == current and row.generation == generation and row.text == fragment.text
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
            INSERT INTO paper_chunks (chunk_id, arxiv_id, source_kind, source_version, chunk_index, source_start, source_end, generation)
            VALUES (:chunk_id, :arxiv_id, :source_kind, :source_version, :chunk_index, :source_start, :source_end, :generation)
        """), [
            {
                **params, "generation": generation, "chunk_id": chunk_id, "chunk_index": index,
                "source_start": fragment.source_start, "source_end": fragment.source_end,
            }
            for index, (chunk_id, fragment) in enumerate(zip(ids, fragments, strict=True))
        ])
    return len(fragments)
