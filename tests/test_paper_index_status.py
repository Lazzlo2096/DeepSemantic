"""Index chips require every current chunk to exist in Qdrant, independently per branch."""
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))
import paper_index_status as indexes
import papers
from fastapi import HTTPException, Response
from processing.config import fingerprint, pipeline_settings, summary_generation


def source_row(kind, chunk_id, **changes):
    settings = pipeline_settings()
    row = {"arxiv_id": "2611.00001v1", "source_kind": kind, "source_version": f"{kind}-v1",
           "markdown_version": "md-v1", "chunk_id": chunk_id}
    row["generation"] = fingerprint([row["source_version"], settings["index"]])
    row["summary_generation"] = summary_generation(row, settings)
    return {**row, **changes}


class PaperIndexTests(unittest.IsolatedAsyncioTestCase):
    async def verify(self, rows, handler):
        session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
            mappings=lambda: SimpleNamespace(all=lambda: rows))))
        @asynccontextmanager
        async def database_session(request):
            yield session
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://qdrant") as client:
            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(qdrant=client)))
            with patch.object(indexes, "database_session", database_session):
                return (await indexes.paper_index_statuses(request, ["2611.00001v1"]))["2611.00001v1"]

    async def test_article_ready_does_not_depend_on_summary_and_checks_actual_points(self):
        def handler(request):
            self.assertIn('"with_vector":false', request.content.decode())
            return httpx.Response(200, json={"result": [{"id": 1}, {"id": 2}]})
        result = await self.verify([source_row("article", 1), source_row("article", 2)], handler)
        self.assertEqual(result["article_index"], {"status": "completed", "chunks_total": 2, "chunks_in_qdrant": 2})
        self.assertEqual(result["summary_index"]["status"], "pending")

    async def test_missing_point_prevents_completion_even_when_chunks_exist(self):
        result = await self.verify([source_row("summary", 1), source_row("summary", 2)],
            lambda request: httpx.Response(200, json={"result": [{"id": 1}]}))
        self.assertEqual(result["summary_index"], {"status": "pending", "chunks_total": 2, "chunks_in_qdrant": 1})

    async def test_summary_text_without_chunks_and_outdated_generations_are_not_ready(self):
        def no_requests(request):
            self.fail("No current chunks should be verified")
        result = await self.verify([source_row("summary", None), source_row("article", 1, generation="old"),
            source_row("summary", 2, summary_generation="old")], no_requests)
        self.assertEqual(result["article_index"]["status"], "pending")
        self.assertEqual(result["summary_index"]["status"], "pending")

    async def test_unavailable_qdrant_is_explicitly_unknown(self):
        def handler(request):
            raise httpx.ConnectError("Unavailable", request=request)
        result = await self.verify([source_row("article", 1)], handler)
        self.assertEqual(result["article_index"]["status"], "unavailable")
        self.assertIsNone(result["article_index"]["chunks_in_qdrant"])

    async def test_missing_collection_is_not_ready(self):
        result = await self.verify([source_row("article", 1)], lambda request: httpx.Response(404))
        self.assertEqual(result["article_index"]["status"], "pending")
        self.assertEqual(result["article_index"]["chunks_in_qdrant"], 0)

    async def test_many_chunks_use_bounded_batches(self):
        import json
        sizes = []
        def handler(request):
            ids = json.loads(request.content)["ids"]
            sizes.append(len(ids))
            return httpx.Response(200, json={"result": [{"id": identity} for identity in ids]})
        result = await self.verify([source_row("article", identity) for identity in range(1, 258)], handler)
        self.assertEqual(sizes, [256, 1])
        self.assertEqual(result["article_index"]["status"], "completed")


class CatalogIndexFilterTests(unittest.IsolatedAsyncioTestCase):
    async def catalog(self, status, *, offset=0, unavailable=False):
        identities = ["pending", "ready-1", "ready-2", "ready-3"]

        async def partitions(size):
            yield identities[:2]
            yield identities[2:]

        candidates = SimpleNamespace(partitions=partitions, close=AsyncMock())
        session = SimpleNamespace(stream_scalars=AsyncMock(return_value=candidates))

        async def execute(statement):
            selected = statement.compile().params["arxiv_id_1"]
            return SimpleNamespace(mappings=lambda: SimpleNamespace(
                all=lambda: [{"arxiv_id": identity} for identity in selected]))

        session.execute = execute

        @asynccontextmanager
        async def database_session(request):
            yield session

        async def statuses(request, selected, *, source_kinds, **kwargs):
            kind, = source_kinds
            return {identity: {f"{kind}_index": {
                "status": "pending" if kind == "summary" or identity == "pending"
                    else "unavailable" if unavailable else "completed",
            }} for identity in selected}

        response = Response()
        with patch.object(papers, "database_session", database_session), \
                patch.object(papers, "paper_index_statuses", statuses):
            result = await papers.list_papers(SimpleNamespace(), response,
                status=status, limit=1, offset=offset)
        candidates.close.assert_awaited_once()
        return result, response

    async def test_ready_filter_counts_before_pagination_and_ignores_pending_summary(self):
        result, response = await self.catalog("article_index_completed", offset=1)
        self.assertEqual(response.headers["X-Total-Count"], "3")
        self.assertEqual([row["arxiv_id"] for row in result], ["ready-2"])
        self.assertEqual(result[0]["article_index"]["status"], "completed")
        self.assertEqual(result[0]["summary_index"]["status"], "pending")

    async def test_ready_filter_preserves_total_on_empty_page(self):
        result, response = await self.catalog("article_index_completed", offset=3)
        self.assertEqual(result, [])
        self.assertEqual(response.headers["X-Total-Count"], "3")

    async def test_pending_filter_remains_supported(self):
        result, response = await self.catalog("article_index_pending")
        self.assertEqual([row["arxiv_id"] for row in result], ["pending"])
        self.assertEqual(response.headers["X-Total-Count"], "1")

    async def test_ready_filter_reports_unavailable_qdrant(self):
        with self.assertRaises(HTTPException) as error:
            await self.catalog("article_index_completed", unavailable=True)
        self.assertEqual(error.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
