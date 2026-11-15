import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import date, datetime
from functools import wraps
from hashlib import sha256
import json
from uuid import UUID

import httpx
from psycopg.types.json import Jsonb
import requests
from sqlalchemy import text
from temporalio import activity
from temporalio.exceptions import ApplicationError

from processing.html_text import extract_markdown
from processing.arxiv import Article, ArxivClient, ImportJob, ensure_bucket, log_import_event, log_import_update, save_paper, store_source
from processing.db import connect_database
from chunks import PAPER_COLLECTIONS
from embedding_schemas import EMBEDDING_OPENROUTER_MODEL
from llm_chat import ChatSettings
from processing.paper_embeddings import BATCH_SIZE, index_batch, replace_source_chunks
from processing.config import CHUNKER_VERSION, SUMMARY_PROMPT_VERSION
from processing.db import connection
from processing.summarizer import Summarizer, SummaryInputError


def heartbeats(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        async def pulse():
            while True:
                activity.heartbeat()
                await asyncio.sleep(10)

        task = asyncio.create_task(pulse())
        try:
            return await function(*args, **kwargs)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    return wrapped


@asynccontextmanager
async def serialized(key):
    # Session-scoped lock also fences a late Activity attempt whose HTTP call is
    # still returning after Temporal has scheduled its replacement.
    async with connection() as db:
        await db.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,))
        try:
            yield db
        finally:
            await db.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))


class Activities:
    def __init__(self, state, tokenizer, max_tokens):
        self.state = state
        self.s3 = state.rustfs
        self.tokenizer = tokenizer
        self.max_tokens = max_tokens

    @activity.defn(name="finish_job")
    async def finish_job(self, p: dict):
        previous = None
        async with connection() as db, db.transaction():
            await db.execute("""
                UPDATE temporal_jobs SET status = %s, finished_at = now(), last_error = %s WHERE id = %s
            """, (p["status"], p.get("error"), p["id"]))
            if p["kind"] == "import":
                status = "failed" if p["status"] == "failed" else "completed"
                previous = await (await db.execute("SELECT status FROM arxiv_imports WHERE id = %s FOR UPDATE",
                                                  (p["import_id"],))).fetchone()
                await db.execute("""
                    UPDATE arxiv_imports SET status = %s::arxiv_import_status, finished_at = now(),
                        lease_until = NULL, claim_id = NULL WHERE id = %s
                """, (status, p["import_id"]))
                await db.execute("""
                    UPDATE arxiv_import_attempts SET finished_at = now() WHERE import_id = %s
                    AND attempt = (SELECT attempts FROM arxiv_imports WHERE id = %s)
                """, (p["import_id"], p["import_id"]))
            elif p["kind"] == "summary" and p["status"] == "failed":
                await db.execute("""
                    UPDATE paper_summaries SET status = 'failed', finished_at = now(), last_error = %s,
                        lease_until = NULL WHERE id = %s AND status <> 'completed'
                """, (p.get("error"), p["summary_id"]))
            elif p["kind"] == "text" and p["status"] == "failed":
                await db.execute("""
                    UPDATE arxiv_papers SET status = 'downloaded', lease_until = NULL
                    WHERE arxiv_id = %s AND jsonb_build_array(bucket, object_key, downloaded_at)::text = %s
                      AND status = 'texting'
                """, (p["arxiv_id"], p["source_version"]))
        if p["kind"] == "import" and previous:
            log_import_update(UUID(p["import_id"]), previous, {"status": status})
            log_import_event("arxiv_import.attempt_finished", ImportJob(UUID(p["import_id"])),
                             status=status, error=p.get("error"))
        activity.logger.info("Job %s finished: %s", p["id"], p["status"])

    @activity.defn(name="import_control")
    @heartbeats
    async def import_control(self, p: dict):
        async with connection() as db, db.transaction():
            row = await (await db.execute("SELECT status FROM arxiv_imports WHERE id = %s FOR UPDATE",
                                         (p["import_id"],))).fetchone()
            if row is None or row["status"] in ("completed", "failed"):
                return "terminal"
            if row["status"] in ("paused", "pausing"):
                await db.execute("UPDATE arxiv_imports SET status = 'paused' WHERE id = %s", (p["import_id"],))
                state = "paused"
            else:
                await db.execute("""
                    UPDATE arxiv_imports SET status = 'running', started_at = coalesce(started_at, now()),
                        attempts = greatest(attempts, 1), lease_until = NULL, claim_id = NULL WHERE id = %s
                """, (p["import_id"],))
                await db.execute("""
                    INSERT INTO arxiv_import_attempts (import_id, attempt, source_format)
                    SELECT id, attempts, %s FROM arxiv_imports WHERE id = %s ON CONFLICT DO NOTHING
                """, (p["source_format"], p["import_id"]))
                state = "running"
        log_import_update(UUID(p["import_id"]), row, {"status": state})
        if row["status"] == "queued":
            log_import_event("arxiv_import.attempt_started", ImportJob(UUID(p["import_id"])), source_format=p["source_format"])
        return state

    @activity.defn(name="import_discover")
    @heartbeats
    async def import_discover(self, p: dict):
        async with serialized(f"import:{p['import_id']}") as db:
            row = await (await db.execute("SELECT temporal_discovered FROM arxiv_imports WHERE id = %s",
                                         (p["import_id"],))).fetchone()
            if row["temporal_discovered"]:
                return True

            def discover():
                with connect_database() as sync_db, requests.Session() as session:
                    sync_db.execute("SELECT pg_advisory_lock(hashtext('deepsemantic.arxiv-grabber'))")
                    return list(ArxivClient(session).articles(
                        date.fromisoformat(p["date_from"]), date.fromisoformat(p["date_to"]), p["query"], lambda size: None,
                    ))

            # Keep the potentially large manifest in PostgreSQL, outside Event History.
            articles = await asyncio.to_thread(discover)
            async with db.transaction():
                async with db.cursor() as cursor:
                    await cursor.executemany("""
                        INSERT INTO arxiv_import_items (import_id, position, article) VALUES (%s, %s, %s)
                        ON CONFLICT DO NOTHING
                    """, [(p["import_id"], index, Jsonb({**asdict(article), "submitted_at": article.submitted_at.isoformat()}))
                          for index, article in enumerate(articles)])
                await db.execute("UPDATE arxiv_imports SET temporal_discovered = true WHERE id = %s", (p["import_id"],))
                await self._import_stats(db, p)
            return True

    async def _import_stats(self, db, p):
        await db.execute("""
            UPDATE arxiv_import_attempts SET
                articles_found = stats.total, processed_articles = stats.processed,
                pdf_available = stats.available, pdf_reused = stats.reused, html_unavailable = stats.unavailable
            FROM (SELECT count(*) AS total, count(outcome) AS processed,
                count(*) FILTER (WHERE outcome IN ('available', 'reused')) AS available,
                count(*) FILTER (WHERE outcome = 'reused') AS reused,
                count(*) FILTER (WHERE outcome = 'unavailable') AS unavailable
                FROM arxiv_import_items WHERE import_id = %s) stats
            WHERE import_id = %s AND attempt = (SELECT attempts FROM arxiv_imports WHERE id = %s)
        """, (p["import_id"], p["import_id"], p["import_id"]))

    @activity.defn(name="import_step")
    @heartbeats
    async def import_step(self, p: dict):
        async with serialized(f"import:{p['import_id']}") as db:
            status = await (await db.execute("SELECT status FROM arxiv_imports WHERE id = %s", (p["import_id"],))).fetchone()
            if status["status"] in ("pausing", "paused"):
                return True
            item = await (await db.execute("""
                SELECT * FROM arxiv_import_items WHERE import_id = %s AND outcome IS NULL ORDER BY position LIMIT 1
            """, (p["import_id"],))).fetchone()
            if item is None:
                return False

            def download():
                article = Article(**{**item["article"], "submitted_at": datetime.fromisoformat(item["article"]["submitted_at"])})
                object_key = f"arxiv/{article.arxiv_id}.{p['source_format']}"
                # Serialize all arXiv HTTP traffic across worker replicas/imports.
                with connect_database() as sync_db, requests.Session() as session:
                    sync_db.execute("SELECT pg_advisory_lock(hashtext('deepsemantic.arxiv-grabber'))")
                    ensure_bucket(self.s3, p["bucket"])
                    stored = store_source(ArxivClient(session), self.s3, p["bucket"], article.arxiv_id, object_key, p["source_format"])
                    if stored is None:
                        return "unavailable"
                    source, reused = stored
                    save_paper(sync_db, article, p["bucket"], object_key, source["LastModified"], ImportJob(UUID(p["import_id"])))
                    return "reused" if reused else "available"

            outcome = await asyncio.to_thread(download)
            async with db.transaction():
                await db.execute("UPDATE arxiv_import_items SET outcome = %s WHERE import_id = %s AND position = %s",
                                 (outcome, p["import_id"], item["position"]))
                await self._import_stats(db, p)
            return True

    @activity.defn(name="process_text")
    @heartbeats
    async def process_text(self, p: dict):
        async with serialized(f"text:{p['arxiv_id']}") as db:
            row = await (await db.execute("""
                SELECT status, text_object_key FROM arxiv_papers WHERE arxiv_id = %s
                  AND jsonb_build_array(bucket, object_key, downloaded_at)::text = %s
            """, (p["arxiv_id"], p["source_version"]))).fetchone()
            if row is None:
                return False
            if row["status"] == "texted":
                return True
            await db.execute("UPDATE arxiv_papers SET status = 'texting' WHERE arxiv_id = %s", (p["arxiv_id"],))

            def extract():
                response = self.s3.get_object(Bucket=p["bucket"], Key=p["object_key"])
                with response["Body"] as body:
                    markdown = extract_markdown(body, f"https://arxiv.org/html/{p['arxiv_id']}").encode("utf-8")
                key = f"arxiv/{p['arxiv_id']}/text-{sha256(markdown).hexdigest()}.md"
                self.s3.put_object(Bucket=p["bucket"], Key=key, Body=markdown, ContentType="text/markdown; charset=utf-8")
                return key

            key = await asyncio.to_thread(extract)
            updated = await db.execute("""
                UPDATE arxiv_papers SET status = 'texted', text_object_key = %s, lease_until = NULL
                WHERE arxiv_id = %s AND jsonb_build_array(bucket, object_key, downloaded_at)::text = %s
            """, (key, p["arxiv_id"], p["source_version"]))
            return bool(updated.rowcount)

    @activity.defn(name="process_summary")
    @heartbeats
    async def process_summary(self, p: dict):
        if p["prompt_version"] != SUMMARY_PROMPT_VERSION:
            raise ApplicationError("Summary prompt changed; retry with the current configuration", non_retryable=True)
        async with serialized(f"summary:{p['arxiv_id']}") as db:
            row = await (await db.execute("""
                SELECT job.status FROM paper_summaries job JOIN arxiv_papers paper USING (arxiv_id)
                WHERE job.id = %s AND job.source_version = %s AND paper.status = 'texted'
                  AND jsonb_build_array(paper.bucket, paper.text_object_key)::text = %s
            """, (p["summary_id"], p["source_version"], p["source_version"]))).fetchone()
            if row is None:
                return False
            if row["status"] == "completed":
                return True
            await db.execute("""
                UPDATE paper_summaries SET status = 'running', attempts = %s, finished_at = NULL,
                    model = %s, prompt_version = %s, last_error = NULL, parts_total = 1, parts_completed = 0
                WHERE id = %s
            """, (activity.info().attempt, p["model"], p["prompt_version"], p["summary_id"]))

            def read_source():
                response = self.s3.get_object(Bucket=p["bucket"], Key=p["text_object_key"])
                with response["Body"] as body:
                    return body.read().decode("utf-8-sig")

            try:
                source = await asyncio.to_thread(read_source)
            except UnicodeDecodeError as error:
                raise ApplicationError("Article is not valid UTF-8", non_retryable=True) from error
            settings = replace(ChatSettings.from_env(openrouter_model_env="SUMMARY_OPENROUTER_MODEL"),
                               model=p["model"], reasoning_effort=p["reasoning_effort"], provider_order=tuple(p["provider_order"]))
            if not settings.configured:
                raise ApplicationError("OPENROUTER_API_KEY and SUMMARY_OPENROUTER_MODEL are required", non_retryable=True)

            async def progress(completed, total):
                activity.heartbeat(completed, total)

            try:
                async with httpx.AsyncClient(base_url=settings.base_url, timeout=httpx.Timeout(300, connect=5, read=1800)) as client:
                    summary = await Summarizer(client, settings).summarize(source, progress)
            except (SummaryInputError, UnicodeDecodeError) as error:
                raise ApplicationError(str(error), non_retryable=True) from error
            updated = await db.execute("""
                UPDATE paper_summaries job SET status = 'completed', summary = %s, finished_at = now(),
                    parts_completed = 1, parts_total = 1, lease_until = NULL
                FROM arxiv_papers paper WHERE job.id = %s AND job.arxiv_id = paper.arxiv_id
                  AND job.source_version = %s AND paper.status = 'texted'
                  AND jsonb_build_array(paper.bucket, paper.text_object_key)::text = %s
            """, (summary, p["summary_id"], p["source_version"], p["source_version"]))
            return bool(updated.rowcount)

    async def _source(self, p):
        async with self.state.db_sessions.begin() as session:
            return (await session.execute(text("""
                SELECT * FROM paper_chunk_sources WHERE arxiv_id = :arxiv_id
                    AND source_kind = :source_kind AND source_version = :source_version
            """), p)).one_or_none()

    @activity.defn(name="prepare_index")
    @heartbeats
    async def prepare_index(self, p: dict):
        if p["max_tokens"] != self.max_tokens or p["model"] != EMBEDDING_OPENROUTER_MODEL or p["chunker"] != CHUNKER_VERSION:
            raise ApplicationError("Index worker configuration differs from the requested configuration", non_retryable=True)
        async with serialized(f"index:{p['entity']}") as db:
            source = await self._source(p)
            if source is None:
                return False
            await replace_source_chunks(self.state, self.s3, source, self.tokenizer, self.max_tokens)
            await db.execute("""
                INSERT INTO paper_index_state (arxiv_id, source_kind, job_id) VALUES (%s, %s, %s)
                ON CONFLICT (arxiv_id, source_kind) DO UPDATE SET job_id = EXCLUDED.job_id
            """, (p["arxiv_id"], p["source_kind"], p["id"]))
            return True

    @activity.defn(name="index_batch")
    @heartbeats
    async def index_batch_activity(self, p: dict):
        async with serialized(f"index:{p['entity']}") as db:
            head = await (await db.execute("SELECT job_id FROM paper_index_state WHERE arxiv_id = %s AND source_kind = %s",
                                          (p["arxiv_id"], p["source_kind"]))).fetchone()
            if head is None or head["job_id"] != p["id"] or await self._source(p) is None:
                return None
            async with self.state.db_sessions.begin() as session:
                batch = (await session.execute(text("""
                    SELECT chunk.id, chunk.text FROM chunks chunk JOIN current_paper_chunks link ON link.chunk_id = chunk.id
                    WHERE link.arxiv_id = :arxiv_id AND link.source_kind = :source_kind
                      AND link.source_version = :source_version AND chunk.id > :cursor
                    ORDER BY chunk.id LIMIT :batch_size
                """), {**p, "batch_size": BATCH_SIZE})).all()
            if not batch:
                return None
            _, failed = await index_batch(self.state, batch, PAPER_COLLECTIONS[p["source_kind"]])
            if failed:
                raise ApplicationError(f"{failed} chunks rejected by the embedding provider", non_retryable=True)
            return batch[-1].id
