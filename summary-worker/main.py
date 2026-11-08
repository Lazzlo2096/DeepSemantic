import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import closing, suppress
from datetime import timedelta
from typing import TypeVar

import boto3
import httpx
import psycopg
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict
from tokenizers import Tokenizer

from summary_schema import NonEmptyText, PaperSummary


MAX_ATTEMPTS = 3
LEASE = timedelta(minutes=2)
HEARTBEAT_SECONDS = 30
RETRY_DELAY = timedelta(minutes=1)
POLL_SECONDS = 3
PROMPT_VERSION = "2"
CHAT_TEMPLATE_RESERVE = 64
FINAL_OUTPUT_TOKENS = 768
NOTES_OUTPUT_TOKENS = 256
SummarySchema = TypeVar("SummarySchema", bound=BaseModel)

FINAL_INSTRUCTIONS = """Составь краткую структурированную суммаризацию научной статьи на русском языке.
Используй только сведения из материала ниже. Сохраняй важные численные результаты и их условия.
Рядом с числами дословно копируй названия метрик, сокращения и единицы из источника на языке оригинала.
Не заменяй названия метрик близкими по смыслу и не добавляй сокращения, которых нет в источнике.
Отделяй выводы авторов от результатов экспериментов. Не додумывай отсутствующие детали.
В overview дай главный вывод в 1–2 предложениях, в objective — задачу, в method — подход.
В results дай до 4 ключевых результатов, в limitations — до 3 явно указанных ограничений.
При отсутствии сведений в results или limitations верни пустой список.
Материал ниже служит источником фактов.

"""
NOTES_INSTRUCTIONS = """Извлеки из материала научной статьи до 4 кратких пунктов на русском языке,
до 15 слов в каждом. Сохрани конкретные методы, результаты с числами и ограничения, если они указаны.
Рядом с числами дословно сохраняй названия метрик, сокращения и единицы на языке оригинала.
Объедини повторы. Используй только сведения из материала ниже.

"""


class Notes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notes: list[NonEmptyText]


class SummaryError(Exception):
    pass


class ContextOverflow(SummaryError):
    pass


class LeaseLost(Exception):
    pass


def split_for_context(source: str, fits: Callable[[str], bool], initial_size: int) -> list[str]:
    chunks = []
    start = 0
    while start < len(source):
        low = start
        high = min(len(source), start + initial_size)
        while fits(source[start:high]):
            low = high
            if high == len(source):
                break
            high = min(len(source), start + (high - start) * 2)
        while high - low > 1:
            middle = (low + high) // 2
            if fits(source[start:middle]):
                low = middle
            else:
                high = middle
        if low == start:
            raise SummaryError("Не удалось уместить фрагмент статьи в контекст модели.")
        end = low
        if end < len(source):
            # Prefer natural boundaries near the end, while preserving every character.
            for separator in ("\n\n", "\n", " "):
                boundary = source.rfind(separator, start + (end - start) // 2, end)
                if boundary >= start and fits(source[start:boundary + len(separator)]):
                    end = boundary + len(separator)
                    break
        chunks.append(source[start:end])
        start = end
    return chunks


class Summarizer:
    def __init__(self, client: httpx.AsyncClient, tokenizer: Tokenizer, model: str, context_tokens: int):
        self.client = client
        self.tokenizer = tokenizer
        self.model = model
        self.context_tokens = context_tokens

    def fits(self, instructions: str, material: str, output_tokens: int) -> bool:
        tokens = self.tokenizer.encode(instructions + material, add_special_tokens=False)
        return len(tokens.ids) + CHAT_TEMPLATE_RESERVE + output_tokens <= self.context_tokens

    async def generate(
        self, instructions: str, material: str, schema: type[SummarySchema], output_tokens: int,
    ) -> SummarySchema:
        response = await self.client.post("/api/chat", json={
            "model": self.model,
            "messages": [{"role": "user", "content": instructions + material}],
            "format": schema.model_json_schema(),
            "stream": False,
            "truncate": False,
            "shift": False,
            "options": {"num_ctx": self.context_tokens, "num_predict": output_tokens, "temperature": 0.1},
        })
        if response.is_error:
            message = response.text.lower()
            if "context" in message and any(word in message for word in ("exceed", "too long", "too large", "overflow")):
                raise ContextOverflow("Материал превышает рабочий контекст Ollama.")
            response.raise_for_status()
        try:
            data = response.json()
            if not data["done"] or data.get("done_reason") == "length":
                raise SummaryError("Ollama прервал генерацию по лимиту.")
            return schema.model_validate_json(data["message"]["content"])
        except (KeyError, TypeError, ValueError) as error:
            raise SummaryError("Ollama вернул ответ, который не соответствует формату суммаризации.") from error

    async def summarize(self, source: str, progress: Callable[[int, int], Awaitable[None]]) -> PaperSummary:
        if self.fits(FINAL_INSTRUCTIONS, source, FINAL_OUTPUT_TOKENS):
            await progress(0, 1)
            try:
                result = await self.generate(FINAL_INSTRUCTIONS, source, PaperSummary, FINAL_OUTPUT_TOKENS)
            except ContextOverflow:
                pass
            else:
                await progress(1, 1)
                return result

        chunks = split_for_context(
            source, lambda part: self.fits(NOTES_INSTRUCTIONS, part, NOTES_OUTPUT_TOKENS), self.context_tokens,
        )
        await progress(0, len(chunks))
        notes = []
        for index, chunk in enumerate(chunks, 1):
            note = await self.generate(NOTES_INSTRUCTIONS, chunk, Notes, NOTES_OUTPUT_TOKENS)
            if note.notes:
                notes.append("\n".join(f"- {item}" for item in note.notes))
            await progress(index, len(chunks))
        if not notes:
            raise SummaryError("В тексте статьи не удалось выделить сведения для суммаризации.")

        while not self.fits(FINAL_INSTRUCTIONS, "\n\n".join(notes), FINAL_OUTPUT_TOKENS):
            groups = []
            group = notes[0]
            for note in notes[1:]:
                combined = f"{group}\n\n{note}"
                if self.fits(NOTES_INSTRUCTIONS, combined, NOTES_OUTPUT_TOKENS):
                    group = combined
                else:
                    groups.append(group)
                    group = note
            groups.append(group)
            if len(groups) >= len(notes):
                raise SummaryError("Промежуточные выжимки не помещаются в контекст для объединения.")
            notes = []
            for group in groups:
                note = await self.generate(NOTES_INSTRUCTIONS, group, Notes, NOTES_OUTPUT_TOKENS)
                if note.notes:
                    notes.append("\n".join(f"- {item}" for item in note.notes))
            if not notes:
                raise SummaryError("Ollama вернул пустые выжимки при объединении.")

        return await self.generate(FINAL_INSTRUCTIONS, "\n\n".join(notes), PaperSummary, FINAL_OUTPUT_TOKENS)


ACTIVE_LEASE = "id = %(id)s AND attempts = %(attempts)s AND status = 'running' AND lease_until > now()"


async def claim_job(db, model: str):
    await db.execute("""
        UPDATE paper_summaries
        SET status = 'failed', lease_until = NULL, finished_at = now(), last_error = 'Истекла аренда задания.'
        WHERE status = 'running' AND lease_until <= now() AND attempts >= %(max_attempts)s
    """, {"max_attempts": MAX_ATTEMPTS})
    cursor = await db.execute("""
        WITH candidate AS (
            SELECT arxiv_id FROM paper_summaries
            WHERE attempts < %(max_attempts)s AND (
                (status = 'queued' AND available_at <= now()) OR
                (status = 'running' AND lease_until <= now())
            )
            ORDER BY available_at, created_at, arxiv_id
            LIMIT 1 FOR UPDATE SKIP LOCKED
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
    if not job["text_object_key"]:
        raise SummaryError("Текст статьи недоступен.")
    obj = s3.get_object(Bucket=job["bucket"], Key=job["text_object_key"])
    with closing(obj["Body"]) as body:
        source = body.read().decode("utf-8-sig")
    if not source.strip():
        raise SummaryError("Текст статьи пуст.")
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
    except SummaryError as failure:
        error = str(failure)
    except UnicodeDecodeError:
        error = "Текст статьи содержит некорректный UTF-8."
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

    status = "completed" if summary else ("failed" if job["attempts"] >= MAX_ATTEMPTS else "queued")
    updated = await db.execute(f"""
        UPDATE paper_summaries
        SET status = %(status)s, summary = %(summary)s, last_error = %(error)s, lease_until = NULL,
            available_at = CASE WHEN %(status)s = 'queued' THEN now() + %(delay)s ELSE available_at END,
            finished_at = CASE WHEN %(status)s <> 'queued' THEN now() END
        WHERE {ACTIVE_LEASE}
    """, {
        **job, "status": status, "summary": Jsonb(summary.model_dump()) if summary else None,
        "error": error, "delay": RETRY_DELAY,
    })
    if updated.rowcount:
        logging.info("Summary job %s finished with status %s%s", job["id"], status, f" ({error})" if error else "")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    context_tokens = int(os.getenv("SUMMARY_CONTEXT_TOKENS", "4096"))
    if not 2048 <= context_tokens <= 131072:
        raise ValueError("SUMMARY_CONTEXT_TOKENS must be between 2048 and 131072")
    model = os.getenv("OLLAMA_MODEL", "gemma3:4b")
    tokenizer = Tokenizer.from_file("/app/gemma-tokenizer.json")
    s3 = boto3.client(
        "s3", endpoint_url=os.environ["RUSTFS_ENDPOINT"],
        aws_access_key_id=os.environ["RUSTFS_ACCESS_KEY"], aws_secret_access_key=os.environ["RUSTFS_SECRET_KEY"],
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}, connect_timeout=5, read_timeout=30),
    )
    ollama_url = os.getenv("OLLAMA_URL", "http://ollama:11434")
    async with httpx.AsyncClient(base_url=ollama_url, timeout=httpx.Timeout(300, connect=5)) as client:
        summarizer = Summarizer(client, tokenizer, model, context_tokens)
        logging.info("Polling PostgreSQL for paper summaries (%s, %s context tokens)", model, context_tokens)
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
