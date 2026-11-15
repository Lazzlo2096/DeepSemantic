"""Branch independence, REST validation, and cooperative deferral contracts."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))
import pipeline
from main import app
from processing import workflows
from processing.activities import Activities


PAPER = {"arxiv_id": "2611.00001v1", "object_key": "paper.html", "bucket": "papers",
         "text_object_key": "text.md", "source_version": "html-v1", "markdown_version": "md-v1"}
SETTINGS = {"summary": {}, "index": {"model": "test"}}


class BranchTests(unittest.IsolatedAsyncioTestCase):
    async def test_deferred_paper_completes_article_without_starting_summary(self):
        async def child(name, *args, **kwargs):
            self.assertEqual(name, "IndexWorkflow")
            return {"status": "completed"}
        with patch.object(workflows.workflow, "patched", return_value=True), \
                patch.object(workflows, "execute", new=AsyncMock(return_value="deferred")), \
                patch.object(workflows, "child", side_effect=child):
            result = await workflows.PaperWorkflow().run({"paper": PAPER, "settings": SETTINGS})
        self.assertEqual(result["article_index"], "completed")
        self.assertEqual(result["summary_index"], "deferred")

    async def test_enabled_paper_does_not_wait_for_summary_completion(self):
        calls = []
        async def child(name, *args, **kwargs):
            calls.append((name, kwargs))
            if name == "SummaryWorkflow":
                self.assertFalse(kwargs["wait"])
                return None
            return {"status": "completed"}
        with patch.object(workflows.workflow, "patched", return_value=True), \
                patch.object(workflows, "execute", new=AsyncMock(return_value="enabled")), \
                patch.object(workflows, "child", side_effect=child):
            result = await workflows.PaperWorkflow().run({"paper": PAPER, "settings": SETTINGS})
        self.assertEqual(result["article_index"], "completed")
        self.assertEqual(result["summary_index"], "queued")
        self.assertEqual(len(calls), 2)

    async def test_deferred_summary_stops_before_index(self):
        with patch.object(workflows, "execute", new=AsyncMock(return_value={"status": "deferred"})), \
                patch.object(workflows, "child", new=AsyncMock()) as child:
            result = await workflows.SummaryWorkflow().run({"paper": PAPER, "settings": SETTINGS})
        self.assertEqual(result["status"], "deferred")
        child.assert_not_called()

    async def test_summary_worker_checks_setting_before_reading_source_or_calling_llm(self):
        activities = Activities(SimpleNamespace(rustfs=None), None, 512, None)
        with patch.object(activities, "pipeline_summary_mode", new=AsyncMock(return_value="deferred")), \
                patch("processing.activities.activity.heartbeat"):
            result = await activities.summarize({})
        self.assertEqual(result, {"status": "deferred"})

    async def test_summary_index_respects_deferral_before_each_batch(self):
        activities = Activities(SimpleNamespace(rustfs=None), None, 512, None)
        with patch.object(activities, "pipeline_summary_mode", new=AsyncMock(return_value="deferred")), \
                patch("processing.activities.activity.heartbeat"):
            payload = {"source": {"source_kind": "summary"}}
            self.assertEqual(await activities.prepare_index(payload), {"status": "deferred"})
            self.assertEqual(await activities.index_batch_activity(payload), {"status": "deferred"})


class RestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = app
        self.api = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"))

    async def test_invalid_modes_and_unknown_settings_are_rejected(self):
        for payload in ({"summary_mode": "off"}, {"summary_mode": "enabled", "skip": True}, {}):
            response = await self.api.put("/pipeline/settings", json=payload)
            self.assertEqual(response.status_code, 422, response.text)

    async def test_put_starts_durable_reconciler_before_committing_and_wakes_after(self):
        events = []
        row = {"summary_mode": "enabled", "updated_at": datetime.now(timezone.utc)}
        handle = SimpleNamespace(signal=AsyncMock(side_effect=lambda *a, **kw: events.append("wake")))
        async def start(*args):
            events.append("start")
            self.assertEqual(args[1], "SummaryReconcileWorkflow")
            return handle
        session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
            mappings=lambda: SimpleNamespace(one=lambda: row))))
        @asynccontextmanager
        async def database_session(request):
            events.append("transaction")
            yield session
            events.append("commit")
        with patch.object(pipeline.temporal_runtime, "start", side_effect=start), \
                patch.object(pipeline, "database_session", database_session):
            response = await self.api.put("/pipeline/settings", json={"summary_mode": "enabled"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(events, ["start", "transaction", "commit", "wake"])
        self.assertEqual(response.json()["summary_mode"], "enabled")

    async def test_processing_exposes_completed_article_with_deferred_summary(self):
        with patch.object(pipeline.repository, "get_paper", new=AsyncMock(return_value=PAPER)), \
                patch.object(pipeline.repository, "get_pipeline_settings", new=AsyncMock(return_value={"summary_mode": "deferred"})), \
                patch.object(pipeline.repository, "get_summary", new=AsyncMock(return_value=None)), \
                patch.object(pipeline.repository, "index_progress", new=AsyncMock(return_value={
                    "status": "completed", "chunks_total": 3, "chunks_indexed": 3})), \
                patch.object(pipeline.temporal_runtime, "describe", new=AsyncMock(return_value=None)):
            response = await self.api.get("/papers/2611.00001v1/processing")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["article_index"]["status"], "completed")
        self.assertEqual(response.json()["summary_index"]["status"], "deferred")


if __name__ == "__main__":
    unittest.main()
