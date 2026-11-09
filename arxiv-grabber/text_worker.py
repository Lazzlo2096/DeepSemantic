import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from uuid import UUID

import psycopg
from botocore.exceptions import BotoCoreError, ClientError

from html_text import extract_markdown
from storage import connect_database, connect_rustfs


POLL_SECONDS = 3
LEASE = timedelta(minutes=5)
RETRY_DELAY = timedelta(minutes=1)


@dataclass(frozen=True)
class Claim:
    arxiv_id: str
    bucket: str
    object_key: str
    lease_until: datetime
    import_id: UUID | None
    old_status: str


def log_status(claim: Claim, old: str, new: str) -> None:
    if old != new:
        print(json.dumps({
            "event": "arxiv_papers.update",
            "table": "arxiv_papers",
            "operation": "update",
            "arxiv_id": claim.arxiv_id,
            "import_id": claim.import_id.hex if claim.import_id else None,
            "changes": {"status": {"old": old, "new": new}},
        }), flush=True)


def claim_paper(db) -> Claim | None:
    row = db.execute("""
        WITH candidate AS (
            SELECT arxiv_id, status FROM arxiv_papers
            WHERE status IN ('downloaded', 'texting') AND lower(right(object_key, 5)) = '.html'
              AND COALESCE(lease_until, text_available_at) <= now()
            ORDER BY COALESCE(lease_until, text_available_at), arxiv_id
            LIMIT 1 FOR UPDATE SKIP LOCKED
        )
        UPDATE arxiv_papers AS paper
        SET status = 'texting', lease_until = now() + %s
        FROM candidate WHERE paper.arxiv_id = candidate.arxiv_id
        RETURNING paper.arxiv_id, paper.bucket, paper.object_key, paper.lease_until,
                  paper.arxiv_import_id, candidate.status
    """, (LEASE,)).fetchone()
    return Claim(*row) if row is not None else None


def store_text(s3, claim: Claim) -> str:
    response = s3.get_object(Bucket=claim.bucket, Key=claim.object_key)
    with response["Body"] as body:
        markdown = extract_markdown(body, f"https://arxiv.org/html/{claim.arxiv_id}").encode("utf-8")
    # A worker with an expired lease cannot overwrite the text another worker publishes.
    key = f"arxiv/{claim.arxiv_id}/text-{sha256(markdown).hexdigest()}.md"
    s3.put_object(Bucket=claim.bucket, Key=key, Body=markdown, ContentType="text/markdown; charset=utf-8")
    return key


def process_paper(db, s3, claim: Claim) -> None:
    log_status(claim, claim.old_status, "texting")
    try:
        key = store_text(s3, claim)
    except (BotoCoreError, ClientError, OSError, ValueError) as error:
        logging.warning("Could not extract text for %s: %s", claim.arxiv_id, error)
        key = None

    status = "texted" if key is not None else "downloaded"
    # The returned deadline identifies this claim. Replacing the source invalidates it.
    updated = db.execute("""
        UPDATE arxiv_papers
        SET status = %s::arxiv_paper_status, text_object_key = %s, lease_until = NULL, text_available_at = now() + %s
        WHERE arxiv_id = %s AND status = 'texting' AND lease_until = %s AND lease_until > now()
    """, (status, key, RETRY_DELAY if key is None else timedelta(0), claim.arxiv_id, claim.lease_until))
    if updated.rowcount:
        log_status(claim, "texting", status)
    else:
        logging.warning("Text extraction lease lost for %s", claim.arxiv_id)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    s3 = connect_rustfs()
    while True:
        try:
            with connect_database() as db:
                while True:
                    claim = claim_paper(db)
                    if claim is None:
                        time.sleep(POLL_SECONDS)
                    else:
                        process_paper(db, s3, claim)
        except psycopg.OperationalError as error:
            logging.warning("Text extraction database connection failed: %s", error)
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
