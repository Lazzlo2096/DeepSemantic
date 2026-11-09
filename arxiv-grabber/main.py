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

from html_text import article_body, html_unavailable
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
    html_unavailable: int = 0
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
    source_format: str


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

    def get(self, url: str, *, allow_not_found: bool = False, **kwargs) -> requests.Response:
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

            if status == 404 and allow_not_found:
                return response

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
                 object_key: str, source_format: str) -> tuple[dict, bool] | None:
    try:
        return s3.head_object(Bucket=bucket, Key=object_key), True
    except ClientError as error:
        if error.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
            raise

    with TemporaryFile() as source:
        with arxiv.get(f"https://arxiv.org/{source_format}/{arxiv_id}",
                       stream=True, allow_not_found=source_format == "html") as response:
            if response.status_code == 404:
                if html_unavailable(response.content, arxiv_id):
                    return None
                response.raise_for_status()
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


def update_running_import(db, job: ImportJob, assignments: str, *, finish: bool = False, **values) -> dict | None:
    # Lock the old row before reading it. The autocommit statement commits before logging.
    update = db.execute(
        f"""
        WITH previous AS (
            SELECT * FROM arxiv_imports
            WHERE id = %(id)s AND claim_id = %(claim_id)s
              AND status IN ('running', 'pausing') AND lease_until > now()
            FOR NO KEY UPDATE
        ), updated AS (
            UPDATE arxiv_imports AS job SET {assignments}
            FROM previous WHERE job.id = previous.id
            RETURNING job.id, job.attempts, job.status,
                      to_jsonb(previous) - 'claim_id' AS previous_row, to_jsonb(job) - 'claim_id' AS current_row
        ), closed_attempt AS (
            UPDATE arxiv_import_attempts AS attempt SET finished_at = now()
            FROM updated WHERE %(finish)s AND updated.status <> 'paused'
              AND attempt.import_id = updated.id AND attempt.attempt = updated.attempts
        )
        SELECT previous_row, current_row FROM updated
        """,
        {"id": job.id, "claim_id": job.claim_id, "finish": finish, **values},
    ).fetchone()
    if update is None:
        return None
    log_import_update(job.id, *update)
    return update[1]


def save_import_progress(db, job: ImportJob, processed: int, result: ImportResult) -> None:
    updated = db.execute(
        """
        WITH claimed AS (
            SELECT id, attempts FROM arxiv_imports
            WHERE id = %(id)s AND claim_id = %(claim_id)s
              AND status IN ('running', 'pausing') AND lease_until > now()
            FOR NO KEY UPDATE
        )
        UPDATE arxiv_import_attempts AS attempt
        SET processed_articles = %(processed)s, articles_found = %(articles_found)s,
            pdf_available = %(pdf_available)s, pdf_reused = %(pdf_reused)s,
            pdf_failures = %(pdf_failures)s, html_unavailable = %(html_unavailable)s
        FROM claimed WHERE attempt.import_id = claimed.id AND attempt.attempt = claimed.attempts
        """,
        {"id": job.id, "claim_id": job.claim_id, "processed": processed, **result.counters()},
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
        stored = store_source(arxiv, s3, bucket, article.arxiv_id, object_key, source_format)
    except (requests.RequestException, BotoCoreError, ClientError, OSError, ValueError) as error:
        result.last_error = f"Could not save {source_format.upper()} for {article.arxiv_id}: {error}"
        log_import_event("arxiv.paper_failed", job, arxiv_id=article.arxiv_id,
                         level="error", stage=source_format, error=result.last_error)
        result.pdf_failures += 1
        return

    if stored is None:
        result.html_unavailable += 1
        log_import_event("arxiv.paper_skipped", job, arxiv_id=article.arxiv_id, reason="html_unavailable")
        return

    source, reused = stored
    check_lease()
    save_paper(db, article, bucket, object_key, source["LastModified"], job)
    result.pdf_available += 1
    result.pdf_reused += int(reused)
    logging.info("%s -> s3://%s/%s", article.arxiv_id, bucket, object_key)


def import_articles(
    date_from: date, date_to: date, bucket: str, query: str, source_format: str,
    *, job: ImportJob | None = None,
) -> ImportResult:
    result = ImportResult()
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
            articles = arxiv.articles(date_from, date_to, query, count_page)
            if job is not None:
                # A fresh search can return different versions or ordering after a pause.
                articles = list(articles)
                save_import_progress(db, job, 0, result)

            check_lease()
            s3 = connect_rustfs()
            ensure_bucket(s3, bucket)
            for processed, article in enumerate(articles, start=1):
                check_lease()
                process_article(db, arxiv, s3, article, bucket, source_format, result, job,
                                lambda: check_lease(allow_pause=False))
                if job is not None:
                    save_import_progress(db, job, processed, result)

            logging.info("Sources available in RustFS: %d. %s failures: %d. HTML unavailable: %d",
                         result.pdf_available, source_format.upper(), result.pdf_failures, result.html_unavailable)
    except PauseRequested:
        return result
    except (psycopg.Error, requests.RequestException, BotoCoreError, ClientError,
            ElementTree.ParseError, KeyError, RuntimeError, ValueError) as error:
        result.last_error = str(error)
        logging.error("%s", result.last_error)
    return result


def claim_import(db, source_format: str) -> ImportJob | None:
    expired = db.execute(
        """
        WITH previous AS (
            SELECT * FROM arxiv_imports
            WHERE lease_until <= now()
              AND (status = 'pausing' OR (status = 'running' AND attempts >= %s))
            FOR NO KEY UPDATE
        ), updated AS (
            UPDATE arxiv_imports AS job
            SET status = (CASE WHEN previous.status = 'pausing' THEN 'paused' ELSE 'failed' END)::arxiv_import_status,
                finished_at = CASE WHEN previous.status = 'running' THEN now() END,
                lease_until = NULL, claim_id = NULL
            FROM previous WHERE job.id = previous.id
            RETURNING job.id, job.attempts, job.status,
                      to_jsonb(previous) - 'claim_id' AS previous_row, to_jsonb(job) - 'claim_id' AS current_row
        ), closed_attempt AS (
            UPDATE arxiv_import_attempts AS attempt SET finished_at = now()
            FROM updated WHERE updated.status = 'failed'
              AND attempt.import_id = updated.id AND attempt.attempt = updated.attempts
        )
        SELECT id, previous_row, current_row FROM updated
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
            SELECT job.*, attempt.import_id IS NOT NULL AS can_resume, attempt.source_format AS previous_format
            FROM arxiv_imports AS job
            LEFT JOIN arxiv_import_attempts AS attempt
              ON attempt.import_id = job.id AND attempt.attempt = job.attempts AND attempt.finished_at IS NULL
            WHERE (job.status = 'queued' AND job.available_at <= now()
                   AND (job.attempts < %(max_attempts)s OR attempt.import_id IS NOT NULL))
               OR (job.status = 'running' AND job.lease_until <= now() AND job.attempts < %(max_attempts)s)
            ORDER BY job.available_at, job.created_at, job.id
            LIMIT 1 FOR UPDATE OF job SKIP LOCKED
        ), claimed AS (
            UPDATE arxiv_imports AS job
            SET status = 'running', claim_id = gen_random_uuid(),
                attempts = CASE WHEN candidate.status = 'queued' AND candidate.can_resume
                                THEN candidate.attempts ELSE candidate.attempts + 1 END,
                started_at = COALESCE(candidate.started_at, now()), lease_until = now() + %(lease)s
            FROM candidate WHERE job.id = candidate.id
            RETURNING job.id, job.date_from, job.date_to, job.bucket, job.query, job.attempts, job.claim_id,
                      candidate.attempts AS previous_attempt, candidate.status AS previous_status,
                      COALESCE(candidate.previous_format, %(source_format)s) AS source_format,
                      to_jsonb(candidate) - ARRAY['claim_id', 'can_resume', 'previous_format'] AS previous_row,
                      to_jsonb(job) - 'claim_id' AS current_row
        ), expired_attempt AS (
            UPDATE arxiv_import_attempts AS attempt SET finished_at = now()
            FROM claimed WHERE claimed.previous_status = 'running'
              AND attempt.import_id = claimed.id AND attempt.attempt = claimed.previous_attempt
        ), active_attempt AS (
            INSERT INTO arxiv_import_attempts (import_id, attempt, source_format)
            SELECT id, attempts, source_format FROM claimed
            ON CONFLICT (import_id, attempt) DO UPDATE SET source_format = EXCLUDED.source_format
            RETURNING source_format
        )
        SELECT id, date_from, date_to, bucket, query, attempts, claim_id, active_attempt.source_format,
               previous_row, current_row
        FROM claimed CROSS JOIN active_attempt
        """,
        {"max_attempts": MAX_JOB_ATTEMPTS, "lease": JOB_LEASE, "source_format": source_format},
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
        finished_at = CASE WHEN previous.status = 'running' AND %(status)s <> 'queued' THEN now() END
        """,
        status=status, delay=JOB_RETRY_DELAY, finish=True,
    )
    if updated:
        log_import_event("arxiv_import.attempt_finished", job, attempt=job.attempts,
                         status=updated["status"], error=result.last_error, **result.counters())
    else:
        logging.warning("Lost lease before saving arXiv import %s attempt %d", job.id.hex, job.attempts)


def process_import(db, job: ImportJob) -> None:
    log_import_event("arxiv_import.attempt_started", job, attempt=job.attempts, bucket=job.bucket,
                     date_from=job.date_from.isoformat(), date_to=job.date_to.isoformat(), source_format=job.source_format)
    with ThreadPoolExecutor(max_workers=1) as executor:
        work = executor.submit(import_articles, job.date_from, job.date_to, job.bucket, job.query,
                               job.source_format, job=job)
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
                    job = claim_import(db, source_format)
                    if job is None:
                        time.sleep(QUEUE_POLL_SECONDS)
                    else:
                        process_import(db, job)
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
