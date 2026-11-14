"""Reconcile durable domain requests into an outbox, then deliver to Temporal.

Only delivery is polled here. Activities, retries and execution ownership belong
to Temporal. A crash between start_workflow and the SQL acknowledgement is safe
because Workflow IDs are stable and duplicate starts are rejected.
"""

import argparse
import asyncio
import logging
import os
from uuid import uuid4

from psycopg.types.json import Jsonb
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from processing.config import CHUNKER_VERSION, QUEUES, SUMMARY_PROMPT_VERSION, fingerprint, summary_model, temporal_client
from processing.db import connection
from llm_chat import DEFAULT_OPENROUTER_PROVIDER_ORDER


async def enqueue(db, kind: str, entity: str, version, data: dict):
    job_id = f"{kind}:{fingerprint([entity, version])}"
    payload = {**data, "id": job_id, "kind": kind, "entity": entity}
    await db.execute("""
        INSERT INTO temporal_jobs (id, kind, entity, payload) VALUES (%s, %s, %s, %s)
        ON CONFLICT DO NOTHING
    """, (job_id, kind, entity, Jsonb(payload)))


async def reconcile(db):
    async for row in await db.cursor().execute("""
        SELECT id::text, date_from::text, date_to::text, bucket, query FROM arxiv_imports
        WHERE status IN ('queued', 'running', 'pausing', 'paused')
    """):
        await enqueue(db, "import", row["id"], row["id"], {
            **row, "import_id": row["id"], "source_format": os.getenv("ARXIV_SOURCE_FORMAT", "html"),
        })

    async for row in await db.cursor().execute("""
        SELECT arxiv_id, bucket, object_key,
            jsonb_build_array(bucket, object_key, downloaded_at)::text AS source_version
        FROM arxiv_papers WHERE status IN ('downloaded', 'texting') AND lower(right(object_key, 5)) = '.html'
    """):
        await enqueue(db, "text", row["arxiv_id"], row["source_version"], row)

    # The source snapshot and request UUID change together. Late results cannot
    # publish over a replacement request, even if an old Activity is still alive.
    await db.execute("""
        INSERT INTO paper_summaries (arxiv_id, source_version, model, prompt_version)
        SELECT arxiv_id, jsonb_build_array(bucket, text_object_key)::text, %s, %s
        FROM arxiv_papers WHERE status = 'texted' AND lower(right(text_object_key, 3)) = '.md'
        ON CONFLICT (arxiv_id) DO UPDATE SET
            id = gen_random_uuid(), source_version = EXCLUDED.source_version,
            model = EXCLUDED.model, prompt_version = EXCLUDED.prompt_version,
            status = 'queued', summary = NULL, attempts = 0, lease_until = NULL,
            available_at = now(), created_at = now(), finished_at = NULL,
            parts_completed = 0, parts_total = 0, last_error = NULL
        WHERE paper_summaries.source_version IS DISTINCT FROM EXCLUDED.source_version
    """, (summary_model(), SUMMARY_PROMPT_VERSION))

    async for row in await db.cursor().execute("""
        SELECT job.id::text AS summary_id, job.arxiv_id, job.source_version,
            coalesce(nullif(job.model, ''), %s) AS model, paper.bucket, paper.text_object_key
        FROM paper_summaries job JOIN arxiv_papers paper USING (arxiv_id)
        WHERE job.status IN ('queued', 'running') AND paper.status = 'texted'
          AND job.source_version = jsonb_build_array(paper.bucket, paper.text_object_key)::text
    """, (summary_model(),)):
        await enqueue(db, "summary", row["arxiv_id"], row["summary_id"], {
            **row, "prompt_version": SUMMARY_PROMPT_VERSION,
            "reasoning_effort": os.getenv("SUMMARY_REASONING_EFFORT", ""),
            "provider_order": [item.strip() for item in os.getenv("OPENROUTER_PROVIDER_ORDER", DEFAULT_OPENROUTER_PROVIDER_ORDER).split(",") if item.strip()],
        })

    index_config = {
        "model": os.getenv("EMBEDDING_OPENROUTER_MODEL", "baai/bge-m3"),
        "max_tokens": int(os.getenv("EMBEDDING_MAX_TOKENS", "512")), "chunker": CHUNKER_VERSION,
    }
    async for row in await db.cursor().execute("SELECT * FROM paper_chunk_sources"):
        entity = f"{row['arxiv_id']}:{row['source_kind']}"
        await enqueue(db, "index", entity, [row["source_version"], index_config], {**row, **index_config})


async def dispatch(db, client):
    rows = await (await db.execute("""
        SELECT id, kind, payload FROM temporal_jobs WHERE status = 'pending' ORDER BY created_at, id LIMIT 100
    """)).fetchall()
    for row in rows:
        try:
            await client.start_workflow(
                "ProcessingWorkflow", row["payload"], id=row["id"], task_queue=QUEUES[row["kind"]],
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            pass
        await db.execute("""
            UPDATE temporal_jobs SET status = 'dispatched', dispatched_at = now(), last_error = NULL
            WHERE id = %s AND status = 'pending'
        """, (row["id"],))


async def retry_job(job_id, *, rebuild=False):
    async with connection() as db, db.transaction():
        row = await (await db.execute("SELECT * FROM temporal_jobs WHERE id = %s FOR UPDATE", (job_id,))).fetchone()
        if row is None or (not rebuild and row["status"] != "failed"):
            raise ValueError("Only an existing failed job can be retried")
        if rebuild and (row["kind"] != "index" or row["status"] not in ("completed", "failed")):
            raise ValueError("Only a completed or failed index job can be rebuilt")
        new_id = f"{job_id}:retry:{uuid4().hex}"
        payload = {key: value for key, value in row["payload"].items() if key not in ("cursor", "prepared")}
        payload["id"] = new_id
        await db.execute("INSERT INTO temporal_jobs (id, kind, entity, payload) VALUES (%s, %s, %s, %s)",
                         (new_id, row["kind"], row["entity"], Jsonb(payload)))
        if row["kind"] == "import":
            await db.execute("UPDATE arxiv_imports SET status = 'queued', finished_at = NULL WHERE id = %s",
                             (payload["import_id"],))
        print(new_id)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--retry", metavar="JOB_ID")
    parser.add_argument("--rebuild", metavar="INDEX_JOB_ID")
    args = parser.parse_args()
    if args.retry or args.rebuild:
        await retry_job(args.retry or args.rebuild, rebuild=bool(args.rebuild))
        return
    client = None
    while True:
        try:
            async with connection() as db:
                await reconcile(db)
                client = client or await temporal_client()
                await dispatch(db, client)
        except Exception:
            logging.exception("Temporal delivery failed; durable requests will be retried")
            client = None
        await asyncio.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
