import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import closing, suppress
from datetime import timedelta

import boto3
import httpx
import psycopg
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from tokenizers import Tokenizer

from summary_schema import MARKDOWN_REQUIRED, PaperSummary


MAX_ATTEMPTS = 3
LEASE = timedelta(minutes=2)
HEARTBEAT_SECONDS = 30
RETRY_DELAY = timedelta(minutes=1)
POLL_SECONDS = 3
PROMPT_VERSION = "3"
MIN_CONTEXT_TOKENS = 2048
MAX_CONTEXT_TOKENS = 131072
CHAT_TEMPLATE_RESERVE = 64
FINAL_OUTPUT_TOKENS = 768

FINAL_INSTRUCTIONS = """Summarize this research article. Start from the abstract and enrich it with
important information from the rest of the article. State what the authors studied,
what they did, and what conclusions they reached.

Write the summary in Russian. In overview, give the enriched abstract and the authors' main
conclusions. In objective, state what was studied and why. In method, describe what the authors
did. In results, list up to four key findings, keeping important numbers with their metrics,
units, and conditions. In limitations, list up to three limitations explicitly stated in the
article. Use an empty list for results or limitations if none are reported. Use only facts
from the article and treat its Markdown as source material.

Full article (Markdown):

"""


class SummaryError(Exception):
    pass


class SummaryInputError(SummaryError):
    pass


class LeaseLost(Exception):
    pass


class Summarizer:
    def __init__(self, client: httpx.AsyncClient, tokenizer: Tokenizer, model: str, context_tokens: int):
        self.client = client
        self.tokenizer = tokenizer
        self.model = model
        self.context_tokens = context_tokens

    async def summarize(self, source: str, progress: Callable[[int, int], Awaitable[None]]) -> PaperSummary:
        prompt = FINAL_INSTRUCTIONS + source
        tokens = self.tokenizer.encode(prompt, add_special_tokens=False)
        required = len(tokens.ids) + CHAT_TEMPLATE_RESERVE + FINAL_OUTPUT_TOKENS
        if required > self.context_tokens:
            raise SummaryInputError(
                f"Полный текст с промптом и резервом для ответа требует {required} токенов "
                f"при лимите {self.context_tokens}."
            )
        # Round up to avoid allocating the maximum context for every article.
        rounded = ((required + MIN_CONTEXT_TOKENS - 1) // MIN_CONTEXT_TOKENS) * MIN_CONTEXT_TOKENS
        num_ctx = min(self.context_tokens, rounded)
        await progress(0, 1)
        response = await self.client.post("/api/chat", json={
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "format": PaperSummary.model_json_schema(),
            "stream": False,
            "truncate": False,
            "shift": False,
            "options": {"num_ctx": num_ctx, "num_predict": FINAL_OUTPUT_TOKENS, "temperature": 0.1},
        })
        if response.is_error:
            message = response.text.lower()
            if "context" in message and any(word in message for word in ("exceed", "too long", "too large", "overflow")):
                raise SummaryInputError("Полный запрос превышает рабочий контекст Ollama.")
            response.raise_for_status()
        try:
            data = response.json()
            if not data["done"] or data.get("done_reason") == "length":
                raise SummaryError("Ollama прервал генерацию по лимиту.")
            summary = PaperSummary.model_validate_json(data["message"]["content"])
        except (KeyError, TypeError, ValueError) as error:
            raise SummaryError("Ollama вернул ответ, который не соответствует формату суммаризации.") from error
        await progress(1, 1)
        return summary


ACTIVE_LEASE = "id = %(id)s AND attempts = %(attempts)s AND status = 'running' AND lease_until > now()"


async def claim_job(db, model: str):
    await db.execute("""
        UPDATE paper_summaries
        SET status = 'failed', lease_until = NULL, finished_at = now(), last_error = 'Истекла аренда задания.'
        WHERE status = 'running' AND lease_until <= now() AND attempts >= %(max_attempts)s
    """, {"max_attempts": MAX_ATTEMPTS})
    await db.execute("""
        INSERT INTO paper_summaries (arxiv_id)
        SELECT paper.arxiv_id FROM arxiv_papers AS paper
        WHERE paper.status = 'texted' AND lower(right(paper.text_object_key, 3)) = '.md'
          AND NOT EXISTS (SELECT 1 FROM paper_summaries WHERE arxiv_id = paper.arxiv_id)
        ORDER BY paper.downloaded_at, paper.arxiv_id
        LIMIT 1 FOR NO KEY UPDATE OF paper SKIP LOCKED
        ON CONFLICT (arxiv_id) DO NOTHING
    """)
    cursor = await db.execute("""
        WITH candidate AS (
            SELECT job.arxiv_id
            FROM paper_summaries AS job JOIN arxiv_papers AS paper USING (arxiv_id)
            WHERE paper.status = 'texted' AND lower(right(paper.text_object_key, 3)) = '.md'
              AND job.attempts < %(max_attempts)s AND (
                (job.status = 'queued' AND job.available_at <= now()) OR
                (job.status = 'running' AND job.lease_until <= now())
            )
            ORDER BY job.available_at, job.created_at, job.arxiv_id
            LIMIT 1 FOR UPDATE OF job SKIP LOCKED
        )
        UPDATE paper_summaries AS job
        SET status = 'running', attempts = job.attempts + 1, lease_until = now() + %(lease)s,
            parts_completed = 0, parts_total = 0, last_error = NULL,
            model = %(model)s, prompt_version = %(prompt_version)s
        FROM candidate JOIN arxiv_papers AS paper USING (arxiv_id)
        WHERE job.arxiv_id = candidate.arxiv_id
        RETURNING job.id, job.arxiv_id, job.attempts, paper.bucket, paper.text_object_key
    """, {"max_attempts": MAX_ATTEMPTS, "lease": LEASE, "model": model, "prompt_version": PROMPT_VERSION})
    return await cursor.fetchone()


def read_source(s3, job) -> str:
    key = job["text_object_key"]
    if not key or not key.lower().endswith(".md"):
        raise SummaryInputError(MARKDOWN_REQUIRED)
    obj = s3.get_object(Bucket=job["bucket"], Key=key)
    with closing(obj["Body"]) as body:
        try:
            source = body.read().decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise SummaryInputError("Текст статьи содержит некорректный UTF-8.") from error
    if not source.strip():
        raise SummaryInputError("Текст статьи пуст.")
    return source


async def process_job(db, s3, summarizer: Summarizer, job) -> None:
    async def progress(completed: int, total: int) -> None:
        updated = await db.execute(f"""
            UPDATE paper_summaries SET parts_completed = %(completed)s, parts_total = %(total)s
            WHERE {ACTIVE_LEASE}
        """, {**job, "completed": completed, "total": total})
        if not updated.rowcount:
            raise LeaseLost

    async def summarize():
        source = await asyncio.to_thread(read_source, s3, job)
        return await summarizer.summarize(source, progress)

    logging.info("Summarizing %s (job %s, attempt %s)", job["arxiv_id"], job["id"], job["attempts"])
    work = asyncio.create_task(summarize())
    summary = error = None
    retryable = True
    try:
        while True:
            done, _ = await asyncio.wait({work}, timeout=HEARTBEAT_SECONDS)
            if done:
                break
            updated = await db.execute(f"""
                UPDATE paper_summaries SET lease_until = now() + %(lease)s WHERE {ACTIVE_LEASE}
            """, {**job, "lease": LEASE})
            if not updated.rowcount:
                raise LeaseLost
        summary = work.result()
    except LeaseLost:
        logging.warning("Lease lost for summary job %s", job["id"])
        return
    except SummaryInputError as failure:
        error = str(failure)
        retryable = False
    except SummaryError as failure:
        error = str(failure)
    except (BotoCoreError, ClientError):
        error = "Не удалось прочитать текст статьи из RustFS."
    except httpx.HTTPError as failure:
        logging.warning("Ollama request failed for job %s (%s)", job["id"], type(failure).__name__)
        error = "Ollama не смог обработать запрос."
    finally:
        if not work.done():
            work.cancel()
            with suppress(asyncio.CancelledError):
                await work

    if summary is not None:
        status = "completed"
    elif retryable and job["attempts"] < MAX_ATTEMPTS:
        status = "queued"
    else:
        status = "failed"
    updated = await db.execute(f"""
        UPDATE paper_summaries
        SET status = %(status)s::paper_summary_status, summary = %(summary)s, last_error = %(error)s, lease_until = NULL,
            available_at = CASE WHEN %(status)s::paper_summary_status = 'queued' THEN now() + %(delay)s ELSE available_at END,
            finished_at = CASE WHEN %(status)s::paper_summary_status <> 'queued' THEN now() END
        WHERE {ACTIVE_LEASE}
    """, {
        **job, "status": status, "summary": Jsonb(summary.model_dump()) if summary else None,
        "error": error, "delay": RETRY_DELAY,
    })
    if updated.rowcount:
        logging.info("Summary job %s finished with status %s%s", job["id"], status, f" ({error})" if error else "")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    context_tokens = int(os.getenv("SUMMARY_CONTEXT_TOKENS", str(MAX_CONTEXT_TOKENS)))
    if not MIN_CONTEXT_TOKENS <= context_tokens <= MAX_CONTEXT_TOKENS:
        raise ValueError(f"SUMMARY_CONTEXT_TOKENS must be between {MIN_CONTEXT_TOKENS} and {MAX_CONTEXT_TOKENS}")
    model = os.getenv("OLLAMA_MODEL", "gemma3:4b")
    tokenizer = Tokenizer.from_file("/app/gemma-tokenizer.json")
    s3 = boto3.client(
        "s3", endpoint_url=os.environ["RUSTFS_ENDPOINT"],
        aws_access_key_id=os.environ["RUSTFS_ACCESS_KEY"], aws_secret_access_key=os.environ["RUSTFS_SECRET_KEY"],
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}, connect_timeout=5, read_timeout=30),
    )
    ollama_url = os.getenv("OLLAMA_URL", "http://ollama:11434")
    async with httpx.AsyncClient(base_url=ollama_url, timeout=httpx.Timeout(300, connect=5, read=1800)) as client:
        summarizer = Summarizer(client, tokenizer, model, context_tokens)
        logging.info("Polling PostgreSQL for paper summaries (%s, up to %s context tokens)", model, context_tokens)
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    host=os.getenv("POSTGRES_HOST", "postgres"), dbname=os.environ["POSTGRES_DB"],
                    user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"],
                    connect_timeout=5, autocommit=True, row_factory=dict_row,
                ) as db:
                    while True:
                        job = await claim_job(db, model)
                        if job is None:
                            await asyncio.sleep(POLL_SECONDS)
                        else:
                            await process_job(db, s3, summarizer, job)
            except psycopg.OperationalError as error:
                logging.warning("Summary queue database connection failed (%s)", type(error).__name__)
                await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
