import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from tempfile import TemporaryFile
from xml.etree import ElementTree

import aio_pika
import boto3
import psycopg
import requests
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pypdf import PdfReader
from pypdf.errors import PyPdfError


ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_QUEUE = "arxiv.imports"
ARXIV_QUEUE_ARGUMENTS = {"x-single-active-consumer": True, "x-consumer-timeout": 86_400_000}
ARXIV_ID = re.compile(r"(?:\d{4}\.\d{4,5}|[a-z][a-z.-]*/\d{7})v[1-9]\d*")
NAMESPACES = {"a": "http://www.w3.org/2005/Atom", "os": "http://a9.com/-/spec/opensearch/1.1/"}
PAGE_SIZE = 100
MAX_RESULTS = 10_000
REQUEST_INTERVAL = 3
CHUNK_SIZE = 1_000
CHUNK_OVERLAP = CHUNK_SIZE * 15 // 100


@dataclass(frozen=True)
class Article:
    arxiv_id: str
    title: str
    authors: list[str]
    abstract: str
    submitted_at: datetime


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

    def articles(self, date_from: date, date_to: date, query: str) -> Iterator[Article]:
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
                yield from self.articles(date_from, midpoint, query)
                yield from self.articles(midpoint + timedelta(days=1), date_to, query)
                return

            entries = feed.findall("a:entry", NAMESPACES)
            for entry in entries:
                article = parse_article(entry)
                submitted = article.submitted_at.date()
                if date_from <= submitted <= date_to:
                    yield article
                elif submitted != next_day:
                    raise RuntimeError(f"arXiv returned {article.arxiv_id} outside the requested dates")

            offset += len(entries)
            if offset >= total:
                return
            if not entries:
                raise RuntimeError(f"arXiv ended pagination at {offset} of {total} results")


def parse_article(entry: ElementTree.Element) -> Article:
    arxiv_id = entry.findtext("a:id", default="", namespaces=NAMESPACES).partition("/abs/")[2]
    if not ARXIV_ID.fullmatch(arxiv_id):
        summary = entry.findtext("a:summary", default="", namespaces=NAMESPACES)
        raise ValueError(f"Invalid arXiv entry: {arxiv_id or summary}")

    published = entry.findtext("a:published", default="", namespaces=NAMESPACES)
    return Article(
        arxiv_id=arxiv_id,
        title=" ".join(entry.findtext("a:title", default="", namespaces=NAMESPACES).split()),
        authors=[" ".join(node.text.split()) for node in entry.findall("a:author/a:name", NAMESPACES) if node.text],
        abstract=" ".join(entry.findtext("a:summary", default="", namespaces=NAMESPACES).split()),
        submitted_at=datetime.fromisoformat(published).astimezone(timezone.utc),
    )


def ensure_bucket(s3, bucket: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as error:
        if error.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
            raise
        s3.create_bucket(Bucket=bucket)


def store_pdf(arxiv: ArxivClient, s3, bucket: str, arxiv_id: str, object_key: str) -> dict:
    try:
        return s3.head_object(Bucket=bucket, Key=object_key)
    except ClientError as error:
        if error.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
            raise

    with TemporaryFile() as pdf:
        with arxiv.get(f"https://arxiv.org/pdf/{arxiv_id}", stream=True) as response:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                pdf.write(chunk)
        pdf.seek(0)
        if pdf.read(5) != b"%PDF-":
            raise ValueError(f"arXiv did not return a PDF for {arxiv_id}")
        pdf.seek(0)
        s3.upload_fileobj(pdf, bucket, object_key, ExtraArgs={"ContentType": "application/pdf"})
    return s3.head_object(Bucket=bucket, Key=object_key)


def store_extracted_text(s3, bucket: str, pdf_key: str, pdf_etag: str) -> str:
    text_key = pdf_key.removesuffix(".pdf") + ".txt"
    source_etag = pdf_etag.strip('"')
    try:
        existing = s3.head_object(Bucket=bucket, Key=text_key)
        if existing["ContentLength"] > 0 and existing["Metadata"].get("source-etag") == source_etag:
            return text_key
    except ClientError as error:
        if error.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
            raise

    with TemporaryFile() as pdf, TemporaryFile() as text:
        response = s3.get_object(Bucket=bucket, Key=pdf_key, IfMatch=pdf_etag)
        with response["Body"] as body:
            shutil.copyfileobj(body, pdf, length=1024 * 1024)
        pdf.seek(0)
        for page in PdfReader(pdf).pages:
            page_text = page.extract_text().strip()
            if page_text:
                text.write(page_text.encode("utf-8") + b"\n\n")
        if not text.tell():
            raise ValueError("PDF has no extractable text")
        text.seek(0)
        s3.upload_fileobj(
            text,
            bucket,
            text_key,
            ExtraArgs={"ContentType": "text/plain; charset=utf-8", "Metadata": {"source-etag": source_etag}},
        )
    return text_key


def split_text(text: str) -> Iterator[str]:
    text = " ".join(text.replace("\x00", "").split())
    for start in range(0, len(text), CHUNK_SIZE - CHUNK_OVERLAP):
        end = start + CHUNK_SIZE
        yield text[start:end]
        if end >= len(text):
            break


def store_paper_chunks(db, s3, arxiv_id: str, bucket: str, text_key: str) -> int:
    # Imports share an advisory lock, and each paper's complete chunk set is written atomically.
    existing = db.execute(
        "SELECT 1 FROM paper_chunks WHERE arxiv_id = %s LIMIT 1",
        (arxiv_id,),
    ).fetchone()
    if existing is not None:
        return 0

    response = s3.get_object(Bucket=bucket, Key=text_key)
    with response["Body"] as body:
        chunks = list(split_text(body.read().decode("utf-8")))
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
    return len(chunks)


def import_articles(date_from: date, date_to: date, bucket: str, query: str, *, wait_for_lock: bool = False) -> int:
    try:
        with psycopg.connect(
            host=os.getenv("POSTGRES_HOST", "postgres"),
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            connect_timeout=5,
            autocommit=True,
        ) as db, requests.Session() as session:
            # Serialize imports in this database to keep arXiv traffic single-connection.
            if wait_for_lock:
                db.execute("SELECT pg_advisory_lock(hashtext('deepsemantic.arxiv-grabber'))")
            else:
                locked, = db.execute("SELECT pg_try_advisory_lock(hashtext('deepsemantic.arxiv-grabber'))").fetchone()
                if not locked:
                    raise RuntimeError("Another arxiv-grabber import is running")

            s3 = boto3.client(
                "s3",
                endpoint_url=os.environ["RUSTFS_ENDPOINT"],
                aws_access_key_id=os.environ["RUSTFS_ACCESS_KEY"],
                aws_secret_access_key=os.environ["RUSTFS_SECRET_KEY"],
                region_name="us-east-1",
                config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
            )
            ensure_bucket(s3, bucket)
            arxiv = ArxivClient(session)
            succeeded = failed = text_failed = chunks_failed = 0
            for article in arxiv.articles(date_from, date_to, query):
                object_key = f"arxiv/{article.arxiv_id}.pdf"
                try:
                    pdf = store_pdf(arxiv, s3, bucket, article.arxiv_id, object_key)
                except (requests.RequestException, BotoCoreError, ClientError, OSError, ValueError) as error:
                    logging.error("%s: %s", article.arxiv_id, error)
                    failed += 1
                    continue

                text_object_key = None
                try:
                    text_object_key = store_extracted_text(s3, bucket, object_key, pdf["ETag"])
                except (BotoCoreError, ClientError, OSError, PyPdfError, ValueError) as error:
                    logging.error("Could not extract text for %s: %s", article.arxiv_id, error)
                    text_failed += 1

                catalog = db.execute(
                    """
                    INSERT INTO arxiv_papers AS paper
                        (arxiv_id, bucket, object_key, title, authors, abstract,
                         submitted_at, downloaded_at, text_object_key)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (arxiv_id)
                    DO UPDATE SET bucket = EXCLUDED.bucket,
                                  object_key = EXCLUDED.object_key,
                                  title = EXCLUDED.title,
                                  authors = EXCLUDED.authors,
                                  abstract = EXCLUDED.abstract,
                                  submitted_at = EXCLUDED.submitted_at,
                                  downloaded_at = EXCLUDED.downloaded_at,
                                  text_object_key = EXCLUDED.text_object_key
                    WHERE paper.bucket = EXCLUDED.bucket
                       OR EXCLUDED.downloaded_at > paper.downloaded_at
                       OR (EXCLUDED.downloaded_at = paper.downloaded_at
                           AND EXCLUDED.bucket < paper.bucket)
                    RETURNING bucket, text_object_key
                    """,
                    (article.arxiv_id, bucket, object_key, article.title, article.authors,
                     article.abstract, article.submitted_at, pdf["LastModified"], text_object_key),
                ).fetchone()
                succeeded += 1
                logging.info("%s -> s3://%s/%s", article.arxiv_id, bucket, object_key)

                if catalog is None:
                    catalog = db.execute(
                        "SELECT bucket, text_object_key FROM arxiv_papers WHERE arxiv_id = %s",
                        (article.arxiv_id,),
                    ).fetchone()
                catalog_bucket, catalog_text_key = catalog
                if catalog_text_key is not None:
                    try:
                        count = store_paper_chunks(db, s3, article.arxiv_id, catalog_bucket, catalog_text_key)
                        if count:
                            logging.info("Saved %d chunks for %s in %s", count, article.arxiv_id, catalog_bucket)
                    except (BotoCoreError, ClientError, OSError, psycopg.Error, ValueError) as error:
                        logging.error("Could not save chunks for %s: %s", article.arxiv_id, error)
                        chunks_failed += 1

            logging.info("Articles available in RustFS: %d. PDF failures: %d. Text failures: %d. Chunk failures: %d",
                         succeeded, failed, text_failed, chunks_failed)
            return int(failed > 0 or text_failed > 0 or chunks_failed > 0)
    except (psycopg.Error, requests.RequestException, BotoCoreError, ClientError,
            ElementTree.ParseError, KeyError, RuntimeError, ValueError) as error:
        logging.error("%s", error)
        return 1


async def run_worker(default_bucket: str) -> None:
    # A broker reconnect can redeliver work while the original download thread is still running.
    import_lock = asyncio.Lock()

    async def process(message: aio_pika.IncomingMessage) -> None:
        job_id = message.message_id or "unknown"
        try:
            payload = json.loads(message.body)
            date_from = date.fromisoformat(payload["date_from"])
            date_to = date.fromisoformat(payload["date_to"])
            bucket = payload.get("bucket", default_bucket)
            query = payload.get("query", "")
            if date_from > date_to or not isinstance(bucket, str) or not bucket or not isinstance(query, str):
                raise ValueError("Invalid dates, bucket or query")
        except (KeyError, TypeError, ValueError) as error:
            logging.error("Invalid arXiv import %s: %s", job_id, error)
            await message.reject(requeue=False)
            return

        async with import_lock:
            logging.info("Starting arXiv import %s (%s to %s, bucket %s)", job_id, date_from, date_to, bucket)
            try:
                result = await asyncio.to_thread(import_articles, date_from, date_to, bucket, query, wait_for_lock=True)
            except Exception:
                logging.exception("Unexpected error in arXiv import %s", job_id)
                result = 1

            if result == 0:
                logging.info("Completed arXiv import %s", job_id)
                await message.ack()
            else:
                logging.error("Failed arXiv import %s", job_id)
                await message.reject(requeue=False)

    async with await aio_pika.connect_robust(os.environ["AMQP_URL"], fail_fast=False) as connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)
        queue = await channel.declare_queue(ARXIV_QUEUE, durable=True, arguments=ARXIV_QUEUE_ARGUMENTS)
        await queue.consume(process)
        logging.info("Consuming %s", ARXIV_QUEUE)
        await asyncio.Future()


def main() -> int:
    parser = argparse.ArgumentParser(description="Download arXiv PDFs by submission date (UTC).")
    parser.add_argument("--worker", action="store_true", help="Consume arXiv import jobs from RabbitMQ")
    parser.add_argument("--from", dest="date_from", type=date.fromisoformat)
    parser.add_argument("--to", dest="date_to", type=date.fromisoformat)
    parser.add_argument("--bucket")
    parser.add_argument("--query", help="Optional arXiv filter, e.g. cat:cs.AI")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    default_bucket = os.getenv("RUSTFS_BUCKET", "arxiv-papers")
    if args.worker:
        if any(value is not None for value in (args.date_from, args.date_to, args.bucket, args.query)):
            parser.error("--worker cannot be combined with import options")
        asyncio.run(run_worker(default_bucket))
        return 0

    if args.date_from is None or args.date_to is None:
        parser.error("--from and --to are required for a direct import")
    if args.date_from > args.date_to:
        parser.error("--from must be on or before --to")
    bucket = args.bucket if args.bucket is not None else default_bucket
    return import_articles(args.date_from, args.date_to, bucket, args.query or "")


if __name__ == "__main__":
    raise SystemExit(main())
