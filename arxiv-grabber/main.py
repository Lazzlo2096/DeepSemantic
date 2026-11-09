import argparse
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from tempfile import TemporaryFile
from uuid import UUID
from xml.etree import ElementTree

import psycopg
import requests
from botocore.exceptions import BotoCoreError, ClientError
from psycopg.types.json import Jsonb

from html_text import article_body
from storage import connect_database, connect_rustfs


ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_ID = re.compile(r"(?:\d{4}\.\d{4,5}|[a-z][a-z.-]*/\d{7})v[1-9]\d*")
NAMESPACES = {"a": "http://www.w3.org/2005/Atom", "os": "http://a9.com/-/spec/opensearch/1.1/"}
PAGE_SIZE = 100
MAX_RESULTS = 10_000
REQUEST_INTERVAL = 3
QUEUE_POLL_SECONDS = 3
JOB_LEASE = timedelta(minutes=2)
HEARTBEAT_SECONDS = 30
MAX_JOB_ATTEMPTS = 3
JOB_RETRY_DELAY = timedelta(minutes=1)


@dataclass(frozen=True)
class Article:
    arxiv_id: str
    title: str
    authors: list[str]
    abstract: str
    submitted_at: datetime


@dataclass
class ImportResult:
    articles_found: int = 0
    pdf_reused: int = 0
    pdf_available: int = 0
    pdf_failures: int = 0
    last_error: str | None = None

    def counters(self) -> dict[str, int]:
        counters = asdict(self)
        del counters["last_error"]
        return counters


@dataclass(frozen=True)
class ImportJob:
    id: UUID
    date_from: date
    date_to: date
    bucket: str
    query: str
    attempts: int
    claim_id: UUID
    candidate_articles: dict | None
    checkpoint: dict


class PauseRequested(Exception):
    pass


def log_import_event(event: str, job: ImportJob | None, **fields) -> None:
    if job is not None:
        fields["import_id"] = job.id.hex
    print(json.dumps({"event": event, **fields}), flush=True)


def log_import_update(job_id: UUID, previous: dict, current: dict) -> None:
    changes = {
        column: {"old": previous[column], "new": value}
        for column, value in current.items()
        if previous[column] != value
    }
    if changes:
        log_import_event("arxiv_import.update", None, table="arxiv_imports", operation="update",
                         import_id=job_id.hex, changes=changes)


def log_status_change(arxiv_id: str, change: tuple[str | None, str] | None, job: ImportJob | None) -> None:
    if change is None:
        return
    old_status, new_status = change
    if old_status != new_status:
        operation = "insert" if old_status is None else "update"
        log_import_event(
            f"arxiv_papers.{operation}", job, table="arxiv_papers", operation=operation,
            arxiv_id=arxiv_id, changes={"status": {"old": old_status, "new": new_status}},
        )


class ArxivClient:
    def __init__(self, session: requests.Session):
        self.session = session
        self.session.headers["User-Agent"] = "DeepSemantic/arxiv-grabber"
        self.next_request = time.monotonic() + REQUEST_INTERVAL

    def get(self, url: str, **kwargs) -> requests.Response:
        for attempt in range(3):
            time.sleep(max(0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + REQUEST_INTERVAL
            try:
                response = self.session.get(url, timeout=(10, 60), **kwargs)
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 2:
                    raise
                continue

            status = response.status_code
            if attempt < 2 and (status == 429 or status >= 500):
                retry_after = response.headers.get("Retry-After", "")
                delay = int(retry_after) if retry_after.isdecimal() else 3 * 2**attempt
                self.next_request = max(self.next_request, time.monotonic() + delay)
                response.close()
                continue

            try:
                response.raise_for_status()
            except requests.HTTPError:
                response.close()
                raise
            return response

        raise RuntimeError(f"No response from {url}")

    def articles(
        self, date_from: date, date_to: date, query: str, on_page: Callable[[int], None],
    ) -> Iterator[Article]:
        # Request through the next midnight to cover the last minute of date_to.
        next_day = date_to + timedelta(days=1) if date_to < date.max else date_to
        lower = date_from.isoformat().replace("-", "") + "0000"
        upper = next_day.isoformat().replace("-", "") + ("0000" if next_day > date_to else "2359")
        search = f"submittedDate:[{lower} TO {upper}]"
        if query:
            search = f"({query}) AND {search}"

        offset = 0
        while True:
            with self.get(ARXIV_API, params={
                "search_query": search,
                "start": offset,
                "max_results": PAGE_SIZE,
                "sortBy": "submittedDate",
                "sortOrder": "ascending",
            }) as response:
                feed = ElementTree.fromstring(response.content)

            total = int(feed.findtext("os:totalResults", default="-1", namespaces=NAMESPACES))
            if total < 0:
                raise ValueError("arXiv response is missing the result count")
            if total >= MAX_RESULTS:
                if date_from == date_to or offset:
                    raise RuntimeError(f"arXiv result limit reached for {date_from} to {date_to}")
                midpoint = date_from + (date_to - date_from) // 2
                yield from self.articles(date_from, midpoint, query, on_page)
                yield from self.articles(midpoint + timedelta(days=1), date_to, query, on_page)
                return

            entries = feed.findall("a:entry", NAMESPACES)
            page = []
            for entry in entries:
                article = parse_article(entry)
                submitted = article.submitted_at.date()
                if date_from <= submitted <= date_to:
                    page.append(article)
                elif submitted != next_day:
                    raise RuntimeError(f"arXiv returned {article.arxiv_id} outside the requested dates")

            on_page(len(page))
            yield from page
            offset += len(entries)
            if offset >= total:
                return
            if not entries:
                raise RuntimeError(f"arXiv ended pagination at {offset} of {total} results")


def parse_article(entry: ElementTree.Element) -> Article:
    arxiv_id = entry.findtext("a:id", default="", namespaces=NAMESPACES).partition("/abs/")[2]
    summary = entry.findtext("a:summary", default="", namespaces=NAMESPACES)
    if not ARXIV_ID.fullmatch(arxiv_id):
        raise ValueError(f"Invalid arXiv entry: {arxiv_id or summary}")

    published = entry.findtext("a:published", default="", namespaces=NAMESPACES)
    return Article(
        arxiv_id=arxiv_id,
        title=" ".join(entry.findtext("a:title", default="", namespaces=NAMESPACES).split()),
        authors=[" ".join(node.text.split()) for node in entry.findall("a:author/a:name", NAMESPACES) if node.text],
        abstract=summary.strip(),
        submitted_at=datetime.fromisoformat(published).astimezone(timezone.utc),
    )


def ensure_bucket(s3, bucket: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as error:
        if error.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
            raise
        s3.create_bucket(Bucket=bucket)


def store_source(arxiv: ArxivClient, s3, bucket: str, arxiv_id: str,
                 object_key: str, source_format: str) -> tuple[dict, bool]:
    try:
        return s3.head_object(Bucket=bucket, Key=object_key), True
    except ClientError as error:
        if error.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
            raise

    with TemporaryFile() as source:
        with arxiv.get(f"https://arxiv.org/{source_format}/{arxiv_id}", stream=True) as response:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                source.write(chunk)
        source.seek(0)
        if source_format == "pdf":
            if source.read(5) != b"%PDF-":
                raise ValueError(f"arXiv did not return a PDF for {arxiv_id}")
            content_type = "application/pdf"
        else:
            article_body(source)
            content_type = "text/html; charset=utf-8"
        source.seek(0)
        s3.upload_fileobj(source, bucket, object_key, ExtraArgs={"ContentType": content_type})
    return s3.head_object(Bucket=bucket, Key=object_key), False


def update_running_import(db, job: ImportJob, assignments: str, **values) -> dict | None:
    # Lock the old row before reading it. The autocommit statement commits before logging.
    update = db.execute(
        f"""
        WITH previous AS (
            SELECT * FROM arxiv_imports
            WHERE id = %(id)s AND claim_id = %(claim_id)s
              AND status IN ('running', 'pausing') AND lease_until > now()
            FOR NO KEY UPDATE
        )
        UPDATE arxiv_imports AS job SET {assignments}
        FROM previous WHERE job.id = previous.id
        RETURNING to_jsonb(previous) - ARRAY['claim_id', 'candidate_articles', 'checkpoint'],
                  to_jsonb(job) - ARRAY['claim_id', 'candidate_articles', 'checkpoint']
        """,
        {"id": job.id, "claim_id": job.claim_id, **values},
    ).fetchone()
    if update is None:
        return None
    log_import_update(job.id, *update)
    return update[1]


def save_checkpoint(db, job: ImportJob, next_index: int, result: ImportResult) -> None:
    counters = result.counters()
    checkpoint = {"next_index": next_index, "counters": counters, "had_error": result.last_error is not None}
    updated = db.execute(
        """
        UPDATE arxiv_imports SET checkpoint = %s, result = %s
        WHERE id = %s AND claim_id = %s AND status IN ('running', 'pausing') AND lease_until > now()
        """,
        (Jsonb(checkpoint), Jsonb(counters), job.id, job.claim_id),
    )
    if not updated.rowcount:
        raise RuntimeError(f"Lost lease for arXiv import {job.id.hex} attempt {job.attempts}")


def save_paper(db, article: Article, bucket: str, object_key: str, downloaded_at: datetime,
               job: ImportJob | None) -> None:
    with db.transaction():
        previous = db.execute(
            "SELECT status FROM arxiv_papers WHERE arxiv_id = %s FOR NO KEY UPDATE",
            (article.arxiv_id,),
        ).fetchone()
        paper = db.execute(
            """
            INSERT INTO arxiv_papers AS paper
                (arxiv_id, bucket, object_key, title, authors, abstract,
                 submitted_at, downloaded_at, arxiv_import_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (arxiv_id)
            DO UPDATE SET bucket = EXCLUDED.bucket,
                          object_key = EXCLUDED.object_key,
                          title = EXCLUDED.title,
                          authors = EXCLUDED.authors,
                          abstract = EXCLUDED.abstract,
                          submitted_at = EXCLUDED.submitted_at,
                          downloaded_at = EXCLUDED.downloaded_at,
                          arxiv_import_id = EXCLUDED.arxiv_import_id,
                          text_object_key = NULL,
                          text_available_at = now(),
                          status = 'downloaded'
            WHERE (paper.bucket, paper.object_key, paper.downloaded_at) IS DISTINCT FROM
                  (EXCLUDED.bucket, EXCLUDED.object_key, EXCLUDED.downloaded_at)
              AND (paper.bucket = EXCLUDED.bucket
                   OR EXCLUDED.downloaded_at > paper.downloaded_at
                   OR (EXCLUDED.downloaded_at = paper.downloaded_at AND EXCLUDED.bucket < paper.bucket))
            RETURNING status
            """,
            (article.arxiv_id, bucket, object_key, article.title, article.authors,
             article.abstract, article.submitted_at, downloaded_at, job.id if job else None),
        ).fetchone()
    if paper is not None:
        log_status_change(article.arxiv_id, (previous[0] if previous else None, paper[0]), job)


def process_article(db, arxiv, s3, article: Article, bucket: str, source_format: str,
                    result: ImportResult, job: ImportJob | None, check_lease: Callable[[], None]) -> None:
    object_key = f"arxiv/{article.arxiv_id}.{source_format}"
    try:
        source, reused = store_source(arxiv, s3, bucket, article.arxiv_id, object_key, source_format)
    except (requests.RequestException, BotoCoreError, ClientError, OSError, ValueError) as error:
        result.last_error = f"Could not save {source_format.upper()} for {article.arxiv_id}: {error}"
        log_import_event("arxiv.paper_failed", job, arxiv_id=article.arxiv_id,
                         level="error", stage=source_format, error=result.last_error)
        result.pdf_failures += 1
        return

    check_lease()
    save_paper(db, article, bucket, object_key, source["LastModified"], job)
    result.pdf_available += 1
    result.pdf_reused += int(reused)
    logging.info("%s -> s3://%s/%s", article.arxiv_id, bucket, object_key)


def import_articles(
    date_from: date, date_to: date, bucket: str, query: str, source_format: str,
    *, job: ImportJob | None = None,
) -> ImportResult:
    checkpoint = job.checkpoint if job is not None else {}
    counters = checkpoint.get("counters", {})
    result = ImportResult(**{name: counters[name] for name in ImportResult().counters() if name in counters})
    if checkpoint.get("had_error"):
        result.last_error = "An article failed before the checkpoint. See arxiv.paper_failed logs."
    try:
        with connect_database() as db, requests.Session() as session:
            def check_lease(*, allow_pause: bool = True) -> None:
                if job is None:
                    return
                row = db.execute(
                    """
                    SELECT status FROM arxiv_imports
                    WHERE id = %s AND claim_id = %s AND status IN ('running', 'pausing') AND lease_until > now()
                    """,
                    (job.id, job.claim_id),
                ).fetchone()
                if row is None:
                    raise RuntimeError(f"Lost lease for arXiv import {job.id.hex} attempt {job.attempts}")
                if allow_pause and row[0] == "pausing":
                    raise PauseRequested

            # Serialize arXiv traffic. Check for pause requests while waiting for the lock.
            while True:
                check_lease()
                locked, = db.execute("SELECT pg_try_advisory_lock(hashtext('deepsemantic.arxiv-grabber'))").fetchone()
                if locked:
                    break
                if job is None:
                    raise RuntimeError("Another arxiv-grabber import is running")
                time.sleep(QUEUE_POLL_SECONDS)

            def count_page(size: int) -> None:
                check_lease()
                result.articles_found += size

            arxiv = ArxivClient(session)
            next_index = checkpoint.get("next_index", 0)
            if job is not None and job.candidate_articles is not None:
                saved_articles = job.candidate_articles["articles"]
                result.articles_found = len(saved_articles)
                if next_index == result.articles_found:
                    return result
                articles = (
                    Article(**(article | {"submitted_at": datetime.fromisoformat(article["submitted_at"])}))
                    for article in saved_articles[next_index:]
                )
            else:
                articles = arxiv.articles(date_from, date_to, query, count_page)
                if job is not None:
                    candidates = list(articles)
                    snapshot = {
                        "source_format": source_format,
                        "articles": [asdict(article) | {"submitted_at": article.submitted_at.isoformat()}
                                     for article in candidates],
                    }
                    updated = update_running_import(
                        db, job, "candidate_articles = %(snapshot)s, result = %(result)s",
                        snapshot=Jsonb(snapshot), result=Jsonb(result.counters()),
                    )
                    if not updated:
                        raise RuntimeError(f"Lost lease for arXiv import {job.id.hex} attempt {job.attempts}")
                    articles = iter(candidates)

            check_lease()
            s3 = connect_rustfs()
            ensure_bucket(s3, bucket)
            for index, article in enumerate(articles, start=next_index):
                check_lease()
                process_article(db, arxiv, s3, article, bucket, source_format, result, job,
                                lambda: check_lease(allow_pause=False))
                if job is not None:
                    save_checkpoint(db, job, index + 1, result)

            logging.info("Sources available in RustFS: %d. %s failures: %d",
                         result.pdf_available, source_format.upper(), result.pdf_failures)
    except PauseRequested:
        return result
    except (psycopg.Error, requests.RequestException, BotoCoreError, ClientError,
            ElementTree.ParseError, KeyError, RuntimeError, ValueError) as error:
        result.last_error = str(error)
        logging.error("%s", result.last_error)
    return result


def claim_import(db) -> ImportJob | None:
    expired = db.execute(
        """
        WITH previous AS (
            SELECT * FROM arxiv_imports
            WHERE lease_until <= now()
              AND (status = 'pausing' OR (status = 'running' AND attempts >= %s))
            FOR NO KEY UPDATE
        )
        UPDATE arxiv_imports AS job
        SET status = (CASE WHEN previous.status = 'pausing' THEN 'paused' ELSE 'failed' END)::arxiv_import_status,
            finished_at = CASE WHEN previous.status = 'running' THEN now() END,
            candidate_articles = CASE WHEN previous.status = 'pausing' THEN previous.candidate_articles END,
            lease_until = NULL, claim_id = NULL
        FROM previous WHERE job.id = previous.id
        RETURNING job.id, to_jsonb(previous) - ARRAY['claim_id', 'candidate_articles', 'checkpoint'],
                  to_jsonb(job) - ARRAY['claim_id', 'candidate_articles', 'checkpoint']
        """,
        (MAX_JOB_ATTEMPTS,),
    ).fetchall()
    for job_id, previous, current in expired:
        log_import_update(job_id, previous, current)
        if current["status"] == "failed":
            log_import_event("arxiv_import.lease_expired", None, import_id=job_id.hex,
                             attempt=current["attempts"], level="error",
                             error="Worker lease expired on the final attempt.")

    claimed = db.execute(
        """
        WITH candidate AS (
            SELECT * FROM arxiv_imports
            WHERE (status = 'queued' AND available_at <= now() AND (attempts < %s OR checkpoint IS NOT NULL))
               OR (status = 'running' AND lease_until <= now() AND attempts < %s)
            ORDER BY available_at, created_at, id
            LIMIT 1 FOR UPDATE SKIP LOCKED
        )
        UPDATE arxiv_imports AS job
        SET status = 'running', claim_id = gen_random_uuid(),
            attempts = CASE WHEN candidate.status = 'queued' AND candidate.checkpoint IS NOT NULL
                            THEN candidate.attempts ELSE candidate.attempts + 1 END,
            checkpoint = COALESCE(candidate.checkpoint, '{}'::jsonb),
            started_at = COALESCE(candidate.started_at, now()), lease_until = now() + %s
        FROM candidate
        WHERE job.id = candidate.id
        RETURNING job.id, job.date_from, job.date_to, job.bucket, job.query, job.attempts,
                  job.claim_id, job.candidate_articles, job.checkpoint,
                  to_jsonb(candidate) - ARRAY['claim_id', 'candidate_articles', 'checkpoint'],
                  to_jsonb(job) - ARRAY['claim_id', 'candidate_articles', 'checkpoint']
        """,
        (MAX_JOB_ATTEMPTS, MAX_JOB_ATTEMPTS, JOB_LEASE),
    ).fetchone()
    if claimed is None:
        return None
    *job_fields, previous, current = claimed
    job = ImportJob(*job_fields)
    log_import_update(job.id, previous, current)
    return job


def finish_import(db, job: ImportJob, result: ImportResult) -> None:
    status = "completed"
    if result.last_error is not None:
        status = "queued" if job.attempts < MAX_JOB_ATTEMPTS else "failed"
    updated = update_running_import(
        db, job,
        """
        status = (CASE WHEN previous.status = 'pausing' THEN 'paused' ELSE %(status)s END)::arxiv_import_status,
        lease_until = NULL, claim_id = NULL,
        available_at = CASE WHEN previous.status = 'running' AND %(status)s = 'queued'
                            THEN now() + %(delay)s ELSE previous.available_at END,
        finished_at = CASE WHEN previous.status = 'running' AND %(status)s <> 'queued' THEN now() END,
        candidate_articles = CASE WHEN previous.status = 'pausing' THEN previous.candidate_articles END,
        checkpoint = CASE WHEN previous.status = 'pausing' OR %(status)s <> 'queued' THEN previous.checkpoint END
        """,
        status=status, delay=JOB_RETRY_DELAY,
    )
    if updated:
        log_import_event("arxiv_import.attempt_finished", job, attempt=job.attempts,
                         status=updated["status"], error=result.last_error, **result.counters())
    else:
        logging.warning("Lost lease before saving arXiv import %s attempt %d", job.id.hex, job.attempts)


def process_import(db, job: ImportJob, source_format: str) -> None:
    if job.candidate_articles is not None:
        source_format = job.candidate_articles["source_format"]
    log_import_event("arxiv_import.attempt_started", job, attempt=job.attempts, bucket=job.bucket,
                     date_from=job.date_from.isoformat(), date_to=job.date_to.isoformat(), source_format=source_format)
    with ThreadPoolExecutor(max_workers=1) as executor:
        work = executor.submit(import_articles, job.date_from, job.date_to, job.bucket, job.query,
                               source_format, job=job)
        while not wait((work,), timeout=HEARTBEAT_SECONDS).done:
            updated = update_running_import(db, job, "lease_until = now() + %(lease)s", lease=JOB_LEASE)
            if not updated:
                logging.warning("Lost lease for arXiv import %s attempt %d", job.id.hex, job.attempts)
                return
        try:
            result = work.result()
        except Exception as error:
            logging.exception("Unexpected error in arXiv import %s", job.id.hex)
            result = ImportResult(last_error=str(error))
        finish_import(db, job, result)


def run_worker(source_format: str) -> None:
    logging.info("Polling PostgreSQL for arXiv imports (%s)", source_format)
    while True:
        try:
            with connect_database() as db:
                while True:
                    job = claim_import(db)
                    if job is None:
                        time.sleep(QUEUE_POLL_SECONDS)
                    else:
                        process_import(db, job, source_format)
        except psycopg.OperationalError as error:
            logging.warning("arXiv queue database connection failed: %s", error)
            time.sleep(QUEUE_POLL_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser(description="Download arXiv papers by submission date (UTC).")
    parser.add_argument("--worker", action="store_true", help="Consume arXiv import jobs from PostgreSQL")
    parser.add_argument("--from", dest="date_from", type=date.fromisoformat)
    parser.add_argument("--to", dest="date_to", type=date.fromisoformat)
    parser.add_argument("--bucket")
    parser.add_argument("--query", help="Optional arXiv filter, e.g. cat:cs.AI")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    source_format = os.getenv("ARXIV_SOURCE_FORMAT", "html").strip().lower()
    if source_format not in {"pdf", "html"}:
        parser.error("ARXIV_SOURCE_FORMAT must be pdf or html")
    default_bucket = os.getenv("RUSTFS_BUCKET", "arxiv-papers")
    if args.worker:
        if any(value is not None for value in (args.date_from, args.date_to, args.bucket, args.query)):
            parser.error("--worker cannot be combined with import options")
        run_worker(source_format)
        return 0

    if args.date_from is None or args.date_to is None:
        parser.error("--from and --to are required for a direct import")
    if args.date_from > args.date_to:
        parser.error("--from must be on or before --to")
    bucket = args.bucket if args.bucket is not None else default_bucket
    result = import_articles(args.date_from, args.date_to, bucket, args.query or "", source_format)
    return int(result.last_error is not None)


if __name__ == "__main__":
    raise SystemExit(main())
