import argparse
import logging
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from tempfile import TemporaryFile
from xml.etree import ElementTree

import boto3
import psycopg
import requests
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


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


def store_pdf(arxiv: ArxivClient, s3, bucket: str, arxiv_id: str, object_key: str) -> datetime:
    try:
        return s3.head_object(Bucket=bucket, Key=object_key)["LastModified"]
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
    return s3.head_object(Bucket=bucket, Key=object_key)["LastModified"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Download arXiv PDFs by submission date (UTC).")
    parser.add_argument("--from", dest="date_from", required=True, type=date.fromisoformat)
    parser.add_argument("--to", dest="date_to", required=True, type=date.fromisoformat)
    parser.add_argument("--bucket", default=os.getenv("RUSTFS_BUCKET", "arxiv-papers"))
    parser.add_argument("--query", default="", help="Optional arXiv filter, e.g. cat:cs.AI")
    args = parser.parse_args()
    if args.date_from > args.date_to:
        parser.error("--from must be on or before --to")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
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
            ensure_bucket(s3, args.bucket)
            arxiv = ArxivClient(session)
            succeeded = failed = 0
            for article in arxiv.articles(args.date_from, args.date_to, args.query):
                object_key = f"arxiv/{article.arxiv_id}.pdf"
                try:
                    downloaded_at = store_pdf(arxiv, s3, args.bucket, article.arxiv_id, object_key)
                except (requests.RequestException, BotoCoreError, ClientError, OSError, ValueError) as error:
                    logging.error("%s: %s", article.arxiv_id, error)
                    failed += 1
                    continue

                db.execute(
                    """
                    INSERT INTO arxiv_downloads
                        (arxiv_id, bucket, date_from, date_to, object_key, title, authors,
                         abstract, submitted_at, downloaded_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (arxiv_id, bucket, date_from, date_to)
                    DO UPDATE SET downloaded_at = EXCLUDED.downloaded_at
                    """,
                    (article.arxiv_id, args.bucket, args.date_from, args.date_to, object_key,
                     article.title, article.authors, article.abstract, article.submitted_at, downloaded_at),
                )
                succeeded += 1
                logging.info("%s -> s3://%s/%s", article.arxiv_id, args.bucket, object_key)

            logging.info("Articles available in RustFS: %d. Failed: %d", succeeded, failed)
            return int(failed > 0)
    except (psycopg.Error, requests.RequestException, BotoCoreError, ClientError,
            ElementTree.ParseError, KeyError, RuntimeError, ValueError) as error:
        logging.error("%s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
