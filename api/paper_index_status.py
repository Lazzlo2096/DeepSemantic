"""Verify both current paper indexes against actual Qdrant points in batches."""
import logging
from typing import Literal

import httpx
from pydantic import BaseModel
from sqlalchemy import bindparam, text

from chunks import PAPER_COLLECTIONS
from database import database_session
from processing.config import fingerprint, pipeline_settings, summary_generation


class PaperIndexStatus(BaseModel):
    status: Literal["pending", "completed", "unavailable"] = "pending"
    chunks_total: int = 0
    chunks_in_qdrant: int | None = 0


async def paper_index_statuses(request, arxiv_ids, *, source_kinds=("article", "summary"), session=None):
    result = {identity: {f"{kind}_index": PaperIndexStatus().model_dump() for kind in source_kinds}
              for identity in arxiv_ids}
    if not result:
        return result
    settings = pipeline_settings()
    statement = text("""
        SELECT source.arxiv_id, source.source_kind, source.source_version,
            jsonb_build_array(file.bucket, paper.text_object_key)::text AS markdown_version,
            summary.generation AS summary_generation, link.chunk_id, link.generation
        FROM paper_chunk_sources source
        JOIN arxiv_papers paper USING (arxiv_id)
        JOIN space_files file ON file.id = paper.space_file_id
        LEFT JOIN paper_summaries summary ON summary.arxiv_id = source.arxiv_id AND source.source_kind = 'summary'
        LEFT JOIN paper_chunks link ON link.arxiv_id = source.arxiv_id
            AND link.source_kind = source.source_kind AND link.source_version = source.source_version
        WHERE source.arxiv_id IN :ids AND source.source_kind IN :kinds
    """).bindparams(bindparam("ids", expanding=True), bindparam("kinds", expanding=True))
    params = {"ids": list(result), "kinds": list(source_kinds)}
    if session is None:
        async with database_session(request) as owned_session:
            rows = (await owned_session.execute(statement, params)).mappings().all()
    else:
        rows = (await session.execute(statement, params)).mappings().all()
    expected = {kind: {} for kind in source_kinds}
    for row in rows:
        kind = row["source_kind"]
        if kind == "summary" and row["summary_generation"] != summary_generation(row, settings):
            continue
        if row["chunk_id"] is None or row["generation"] != fingerprint([row["source_version"], settings["index"]]):
            continue
        expected[kind].setdefault(row["arxiv_id"], set()).add(row["chunk_id"])
    for kind, documents in expected.items():
        ids = sorted({identity for chunks in documents.values() for identity in chunks})
        found = set()
        unavailable = False
        try:
            for cursor in range(0, len(ids), 256):
                response = await request.app.state.qdrant.post(
                    f"/collections/{PAPER_COLLECTIONS[kind]}/points",
                    json={"ids": ids[cursor:cursor + 256], "with_payload": False, "with_vector": False},
                )
                if response.status_code == 404:
                    found.clear()
                    break
                response.raise_for_status()
                points = response.json()["result"]
                if not isinstance(points, list):
                    raise ValueError("Invalid Qdrant point list")
                found.update(point["id"] for point in points)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as error:
            logging.getLogger(__name__).warning("Qdrant index verification unavailable: %s", type(error).__name__)
            unavailable = True
        for identity, chunks in documents.items():
            count = len(chunks & found)
            result[identity][f"{kind}_index"] = {
                "status": "unavailable" if unavailable else "completed" if count == len(chunks) else "pending",
                "chunks_total": len(chunks), "chunks_in_qdrant": None if unavailable else count,
            }
    return result
