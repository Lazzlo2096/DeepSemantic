import argparse
import json
import logging
import os
import re
import shutil
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from tempfile import TemporaryFile
from uuid import UUID
from xml.etree import ElementTree

import boto3
import psycopg
import requests
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from html_text import article_body, extract_markdown, split_markdown


ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_ID = re.compile(r"(?:\d{4}\.\d{4,5}|[a-z][a-z.-]*/\d{7})v[1-9]\d*")
NAMESPACES = {"a": "http://www.w3.org/2005/Atom", "os": "http://a9.com/-/spec/opensearch/1.1/"}
PAGE_SIZE = 100
MAX_RESULTS = 10_000
REQUEST_INTERVAL = 3
CHUNK_SIZE = 1_000
CHUNK_OVERLAP = CHUNK_SIZE * 15 // 100
QUEUE_POLL_SECONDS = 3
JOB_LEASE = timedelta(minutes=2)
HEARTBEAT_SECONDS = 30
MAX_JOB_ATTEMPTS = 3
JOB_RETRY_DELAY = timedelta(minutes=1)
LEASE_CHECK_INTERVAL = 3


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
    text_failures: int = 0
    chunk_failures: int = 0
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


def store_extracted_text(s3, bucket: str, object_key: str, etag: str, arxiv_id: str) -> str:
    is_html = object_key.endswith(".html")
    text_key = object_key.rsplit(".", 1)[0] + (".md" if is_html else ".txt")
    source_etag = etag.strip('"')
    try:
        existing = s3.head_object(Bucket=bucket, Key=text_key)
        if existing["ContentLength"] > 0 and existing["Metadata"].get("source-etag") == source_etag:
            return text_key
    except ClientError as error:
        if error.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
            raise

    with TemporaryFile() as source, TemporaryFile() as text:
        response = s3.get_object(Bucket=bucket, Key=object_key, IfMatch=etag)
        with response["Body"] as body:
            shutil.copyfileobj(body, source, length=1024 * 1024)
        source.seek(0)
        if is_html:
            text.write(extract_markdown(source, f"https://arxiv.org/html/{arxiv_id}").encode("utf-8"))
        else:
            for page in PdfReader(source).pages:
                page_text = page.extract_text().strip()
                if page_text:
                    try:
                        encoded_page = page_text.encode("utf-8")
                    except UnicodeEncodeError:
                        # Recover valid surrogate pairs and replace isolated surrogates with U+FFFD.
                        page_text = page_text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
                        encoded_page = page_text.encode("utf-8")
                    text.write(encoded_page + b"\n\n")
        if not text.tell():
            raise ValueError("PDF has no extractable text")
        text.seek(0)
        s3.upload_fileobj(
            text,
            bucket,
            text_key,
            ExtraArgs={
                "ContentType": "text/markdown; charset=utf-8" if is_html else "text/plain; charset=utf-8",
                "Metadata": {"source-etag": source_etag},
            },
        )
    return text_key


def split_text(text: str) -> Iterator[str]:
    text = " ".join(text.replace("\x00", "").split())
    for start in range(0, len(text), CHUNK_SIZE - CHUNK_OVERLAP):
        end = start + CHUNK_SIZE
        yield text[start:end]
        if end >= len(text):
            break


def store_paper_chunks(db, s3, arxiv_id: str, bucket: str, text_key: str, job: ImportJob | None) -> int:
    # Imports share an advisory lock, and each paper's complete chunk set is written atomically.
    existing = db.execute(
        "SELECT 1 FROM paper_chunks WHERE arxiv_id = %s LIMIT 1",
        (arxiv_id,),
    ).fetchone()
    if existing is not None:
        return 0

    response = s3.get_object(Bucket=bucket, Key=text_key)
    with response["Body"] as body:
        text = body.read().decode("utf-8")
    chunks = list(split_markdown(text, CHUNK_SIZE) if text_key.endswith(".md") else split_text(text))
    if not chunks:
        raise ValueError("Extracted text has no chunk content")

    with db.transaction(), db.cursor() as cursor:
        cursor.executemany(
            """
            WITH new_chunk AS (INSERT INTO chunks (text) VALUES (%s) RETURNING id)
            INSERT INTO paper_chunks (chunk_id, arxiv_id, chunk_index)
            SELECT id, %s, %s FROM new_chunk
            """,
            ((text, arxiv_id, index) for index, text in enumerate(chunks)),
        )
        change = cursor.execute("SELECT * FROM refresh_arxiv_paper_status_change(%s)", (arxiv_id,)).fetchone()
    log_status_change(arxiv_id, change, job)
    return len(chunks)


def connect_database():
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "postgres"),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        connect_timeout=5,
        autocommit=True,
    )


def update_running_import(db, job: ImportJob, assignments: str, **values) -> bool:
    # Lock the old row before reading it. The autocommit statement commits before logging.
    update = db.execute(
        f"""
        WITH previous AS (
            SELECT * FROM arxiv_imports
            WHERE id = %(id)s AND attempts = %(attempts)s AND status = 'running' AND lease_until > now()
            FOR NO KEY UPDATE
        )
        UPDATE arxiv_imports AS job SET {assignments}
        FROM previous WHERE job.id = previous.id
        RETURNING to_jsonb(previous), to_jsonb(job)
        """,
        {"id": job.id, "attempts": job.attempts, **values},
    ).fetchone()
    if update is None:
        return False
    log_import_update(job.id, *update)
    return True


def save_paper(db, article: Article, bucket: str, object_key: str, downloaded_at: datetime,
               text_object_key: str | None, job: ImportJob | None) -> tuple[str, str | None] | None:
    with db.transaction():
        previous = db.execute(
            "SELECT status FROM arxiv_papers WHERE arxiv_id = %s FOR NO KEY UPDATE",
            (article.arxiv_id,),
        ).fetchone()
        catalog = db.execute(
            """
            INSERT INTO arxiv_papers AS paper
                (arxiv_id, bucket, object_key, title, authors, abstract,
                 submitted_at, downloaded_at, arxiv_import_id, text_object_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (arxiv_id)
            DO UPDATE SET bucket = EXCLUDED.bucket,
                          object_key = EXCLUDED.object_key,
                          title = EXCLUDED.title,
                          authors = EXCLUDED.authors,
                          abstract = EXCLUDED.abstract,
                          submitted_at = EXCLUDED.submitted_at,
                          downloaded_at = EXCLUDED.downloaded_at,
                          arxiv_import_id = CASE
                              WHEN (paper.bucket, paper.object_key, paper.downloaded_at) =
                                   (EXCLUDED.bucket, EXCLUDED.object_key, EXCLUDED.downloaded_at)
                              THEN paper.arxiv_import_id ELSE EXCLUDED.arxiv_import_id
                          END,
                          text_object_key = COALESCE(EXCLUDED.text_object_key, CASE
                              WHEN (paper.bucket, paper.object_key, paper.downloaded_at) =
                                   (EXCLUDED.bucket, EXCLUDED.object_key, EXCLUDED.downloaded_at)
                              THEN paper.text_object_key
                          END),
                          status = CASE
                              WHEN (paper.bucket, paper.object_key, paper.downloaded_at) =
                                   (EXCLUDED.bucket, EXCLUDED.object_key, EXCLUDED.downloaded_at)
                              THEN paper.status ELSE 'downloaded'
                          END
            WHERE paper.bucket = EXCLUDED.bucket
               OR EXCLUDED.downloaded_at > paper.downloaded_at
               OR (EXCLUDED.downloaded_at = paper.downloaded_at
                   AND EXCLUDED.bucket < paper.bucket)
            RETURNING bucket, text_object_key, status
            """,
            (article.arxiv_id, bucket, object_key, article.title, article.authors,
             article.abstract, article.submitted_at, downloaded_at,
             job.id if job is not None else None, text_object_key),
        ).fetchone()
        change = None
        if catalog is not None:
            change = db.execute(
                "SELECT new_status FROM refresh_arxiv_paper_status_change(%s)", (article.arxiv_id,),
            ).fetchone()
    if catalog is None:
        return None
    catalog_bucket, text_object_key, status = catalog
    log_status_change(article.arxiv_id, (previous[0] if previous else None, change[0] if change else status), job)
    return catalog_bucket, text_object_key


def publish_source(db, s3, article: Article, bucket: str, object_key: str, downloaded_at: datetime,
                   text_object_key: str | None, job: ImportJob | None) -> int:
    previous = db.execute(
        "SELECT object_key FROM arxiv_papers WHERE arxiv_id = %s", (article.arxiv_id,),
    ).fetchone()
    replacing = previous is not None and previous[0].rsplit(".", 1)[-1] != object_key.rsplit(".", 1)[-1]
    if replacing and text_object_key is None:
        return 0

    retired = []
    with db.transaction() if replacing else nullcontext():
        if replacing:
            db.execute("SELECT pg_advisory_xact_lock(hashtext('deepsemantic.paper-embeddings'))")
        catalog = save_paper(db, article, bucket, object_key, downloaded_at, text_object_key, job)
        if catalog is None:
            catalog = db.execute(
                "SELECT bucket, text_object_key FROM arxiv_papers WHERE arxiv_id = %s", (article.arxiv_id,),
            ).fetchone()
        elif replacing:
            retired = db.execute("""
                WITH unlinked AS (
                    DELETE FROM paper_chunks WHERE arxiv_id = %s RETURNING chunk_id
                )
                DELETE FROM chunks USING unlinked WHERE chunks.id = unlinked.chunk_id
                RETURNING qdrant_collection, qdrant_point_id
            """, (article.arxiv_id,)).fetchall()
        catalog_bucket, catalog_text_key = catalog
        count = 0
        if catalog_text_key is not None:
            count = store_paper_chunks(db, s3, article.arxiv_id, catalog_bucket, catalog_text_key, job)

    points_by_collection = {}
    for collection, point in retired:
        if collection is not None and point is not None:
            points_by_collection.setdefault(collection, []).append(point)
    qdrant_url = os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")
    for collection, points in points_by_collection.items():
        try:
            with requests.post(
                f"{qdrant_url}/collections/{collection}/points/delete",
                params={"wait": "true"}, json={"points": points}, timeout=10,
            ) as response:
                if response.status_code != 404:
                    response.raise_for_status()
        except requests.RequestException as error:
            logging.warning("Could not delete retired embeddings for %s: %s", article.arxiv_id, error)
    return count


def import_articles(
    date_from: date, date_to: date, bucket: str, query: str, source_format: str,
    *, job: ImportJob | None = None,
) -> ImportResult:
    result = ImportResult()
    try:
        with connect_database() as db, requests.Session() as session:
            # Serialize imports in this database to keep arXiv traffic single-connection.
            if job is not None:
                db.execute("SELECT pg_advisory_lock(hashtext('deepsemantic.arxiv-grabber'))")
            else:
                locked, = db.execute("SELECT pg_try_advisory_lock(hashtext('deepsemantic.arxiv-grabber'))").fetchone()
                if not locked:
                    raise RuntimeError("Another arxiv-grabber import is running")

            next_lease_check = 0.0

            def check_lease(*, force: bool = False) -> None:
                nonlocal next_lease_check
                if job is None or (not force and time.monotonic() < next_lease_check):
                    return
                active = db.execute(
                    """
                    SELECT 1 FROM arxiv_imports
                    WHERE id = %s AND attempts = %s AND status = 'running' AND lease_until > now()
                    """,
                    (job.id, job.attempts),
                ).fetchone()
                if active is None:
                    raise RuntimeError(f"Lost lease for arXiv import {job.id.hex} attempt {job.attempts}")
                next_lease_check = time.monotonic() + LEASE_CHECK_INTERVAL

            def count_page(size: int) -> None:
                result.articles_found += size
                check_lease(force=True)

            arxiv = ArxivClient(session)
            articles = arxiv.articles(date_from, date_to, query, count_page)
            if job is not None:
                candidates = list(articles)
                updated = update_running_import(
                    db, job, "candidate_arxiv_ids = %(ids)s::text[]",
                    ids=[article.arxiv_id for article in candidates],
                )
                if not updated:
                    raise RuntimeError(f"Lost lease for arXiv import {job.id.hex} attempt {job.attempts}")
                articles = iter(candidates)

            s3 = boto3.client(
                "s3",
                endpoint_url=os.environ["RUSTFS_ENDPOINT"],
                aws_access_key_id=os.environ["RUSTFS_ACCESS_KEY"],
                aws_secret_access_key=os.environ["RUSTFS_SECRET_KEY"],
                region_name="us-east-1",
                config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
            )
            ensure_bucket(s3, bucket)
            for article in articles:
                object_key = f"arxiv/{article.arxiv_id}.{source_format}"
                try:
                    source, reused = store_source(arxiv, s3, bucket, article.arxiv_id, object_key, source_format)
                except (requests.RequestException, BotoCoreError, ClientError, OSError, ValueError) as error:
                    result.last_error = f"Could not save {source_format.upper()} for {article.arxiv_id}: {error}"
                    log_import_event("arxiv.paper_failed", job, arxiv_id=article.arxiv_id,
                                     level="error", stage=source_format, error=result.last_error)
                    result.pdf_failures += 1
                    check_lease()
                    continue

                result.pdf_available += 1
                result.pdf_reused += int(reused)
                check_lease()
                logging.info("%s -> s3://%s/%s", article.arxiv_id, bucket, object_key)

                text_object_key = None
                try:
                    text_object_key = store_extracted_text(s3, bucket, object_key, source["ETag"], article.arxiv_id)
                except (BotoCoreError, ClientError, OSError, PyPdfError, ValueError) as error:
                    result.last_error = f"Could not extract text for {article.arxiv_id}: {error}"
                    log_import_event("arxiv.paper_failed", job, arxiv_id=article.arxiv_id,
                                     level="error", stage="text", error=result.last_error)
                    result.text_failures += 1

                try:
                    count = publish_source(db, s3, article, bucket, object_key, source["LastModified"],
                                           text_object_key, job)
                    if count:
                        logging.info("Saved %d chunks for %s in %s", count, article.arxiv_id, bucket)
                except (BotoCoreError, ClientError, OSError, psycopg.Error, ValueError) as error:
                    result.last_error = f"Could not save chunks for {article.arxiv_id}: {error}"
                    log_import_event("arxiv.paper_failed", job, arxiv_id=article.arxiv_id,
                                     level="error", stage="chunks", error=result.last_error)
                    result.chunk_failures += 1
                check_lease()

            logging.info("Articles available in RustFS: %d. %s failures: %d. Text failures: %d. Chunk failures: %d",
                         result.pdf_available, source_format.upper(), result.pdf_failures,
                         result.text_failures, result.chunk_failures)
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
            WHERE status = 'running' AND lease_until <= now() AND attempts >= %s
            FOR NO KEY UPDATE
        )
        UPDATE arxiv_imports AS job
        SET status = 'failed', finished_at = now(), lease_until = NULL
        FROM previous WHERE job.id = previous.id
        RETURNING job.id, to_jsonb(previous), to_jsonb(job)
        """,
        (MAX_JOB_ATTEMPTS,),
    ).fetchall()
    for job_id, previous, current in expired:
        log_import_update(job_id, previous, current)
        log_import_event("arxiv_import.lease_expired", None, import_id=job_id.hex,
                         attempt=current["attempts"], level="error",
                         error="Worker lease expired on the final attempt.")

    claimed = db.execute(
        """
        WITH candidate AS (
            SELECT * FROM arxiv_imports
            WHERE attempts < %s AND (
                (status = 'queued' AND available_at <= now()) OR
                (status = 'running' AND lease_until <= now())
            )
            ORDER BY available_at, created_at, id
            LIMIT 1 FOR UPDATE SKIP LOCKED
        )
        UPDATE arxiv_imports AS job
        SET status = 'running', attempts = candidate.attempts + 1,
            started_at = COALESCE(candidate.started_at, now()), lease_until = now() + %s
        FROM candidate
        WHERE job.id = candidate.id
        RETURNING job.id, job.date_from, job.date_to, job.bucket, job.query, job.attempts,
                  to_jsonb(candidate), to_jsonb(job)
        """,
        (MAX_JOB_ATTEMPTS, JOB_LEASE),
    ).fetchone()
    if claimed is None:
        return None
    job_id, date_from, date_to, bucket, query, attempts, previous, current = claimed
    log_import_update(job_id, previous, current)
    return ImportJob(job_id, date_from, date_to, bucket, query, attempts)


def finish_import(db, job: ImportJob, result: ImportResult) -> None:
    status = "completed"
    if result.last_error is not None:
        status = "queued" if job.attempts < MAX_JOB_ATTEMPTS else "failed"
    updated = update_running_import(
        db, job,
        """
        status = %(status)s, lease_until = NULL,
        available_at = CASE WHEN %(status)s = 'queued' THEN now() + %(delay)s ELSE previous.available_at END,
        finished_at = CASE WHEN %(status)s <> 'queued' THEN now() END
        """,
        status=status, delay=JOB_RETRY_DELAY,
    )
    if updated:
        log_import_event("arxiv_import.attempt_finished", job, attempt=job.attempts,
                         status=status, error=result.last_error, **result.counters())
    else:
        logging.warning("Lost lease before saving arXiv import %s attempt %d", job.id.hex, job.attempts)


def process_import(db, job: ImportJob, source_format: str) -> None:
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
    source_format = os.getenv("ARXIV_SOURCE_FORMAT", "pdf").strip().lower()
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
