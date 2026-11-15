import argparse
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from tempfile import TemporaryFile
from uuid import UUID
from xml.etree import ElementTree

import requests
from botocore.exceptions import BotoCoreError, ClientError

from processing.html_text import article_body, html_unavailable
from processing.db import connect_database
from storage import connect_rustfs


ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_ID = re.compile(r"(?:\d{4}\.\d{4,5}|[a-z][a-z.-]*/\d{7})v[1-9]\d*")
NAMESPACES = {"a": "http://www.w3.org/2005/Atom", "os": "http://a9.com/-/spec/opensearch/1.1/"}
PAGE_SIZE = 100
MAX_RESULTS = 10_000
REQUEST_INTERVAL = 3


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
                          lease_until = NULL,
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


def import_articles(date_from, date_to, bucket, query, source_format) -> ImportResult:
    """One-off CLI import. Background execution uses processing.activities."""
    result = ImportResult()
    with connect_database() as db, requests.Session() as session:
        arxiv = ArxivClient(session)
        s3 = connect_rustfs()
        try:
            ensure_bucket(s3, bucket)
            def count_page(size):
                result.articles_found += size
            for article in arxiv.articles(date_from, date_to, query, count_page):
                process_article(db, arxiv, s3, article, bucket, source_format, result, None, lambda: None)
        finally:
            s3.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Download arXiv papers by submission date (UTC).")
    parser.add_argument("--worker", action="store_true", help="Run the Temporal import worker")
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
        parser.error("Use docker compose up -d arxiv-grabber to start the Temporal worker")

    if args.date_from is None or args.date_to is None:
        parser.error("--from and --to are required for a direct import")
    if args.date_from > args.date_to:
        parser.error("--from must be on or before --to")
    bucket = args.bucket if args.bucket is not None else default_bucket
    result = import_articles(args.date_from, args.date_to, bucket, args.query or "", source_format)
    return int(result.last_error is not None)


if __name__ == "__main__":
    raise SystemExit(main())
