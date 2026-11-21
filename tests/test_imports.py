"""Import discovery has an unknown total until the manifest is available."""
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))
import main


class ImportResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_preserves_unknown_total_and_accepts_zero_and_discovered_totals(self):
        now = datetime.now(timezone.utc)
        row = {"id": uuid4(), "date_from": date(2026, 11, 1), "date_to": date(2026, 11, 1),
               "bucket": "papers", "query": "", "workflow_id": "import:test", "created_at": now}
        session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
            mappings=lambda: SimpleNamespace(all=lambda: [row]))))
        @asynccontextmanager
        async def database_session(request):
            yield session
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test", headers={
            "X-Auth-Proxy-Secret": "deepsemantic-local-proxy-secret",
            "X-Authenticated-User": "00000000-0000-0000-0000-000000000001",
            "X-Authenticated-Email": "admin@example.test",
            "X-Authenticated-Role": "admin",
        }) as api:
            for total in (None, 0, 12):
                with self.subTest(total=total), patch.object(main, "database_session", database_session), \
                        patch.object(main.temporal_runtime, "describe", new=AsyncMock(return_value={
                            "status": "running", "started_at": now, "finished_at": None,
                            "progress": {"articles_found": total, "attempts": 1, "processed_articles": 0},
                        })):
                    response = await api.get("/arxiv/imports")
                    self.assertEqual(response.status_code, 200, response.text)
                    result = response.json()[0]["result"]
                    self.assertEqual(result["articles_found"], total)
                    self.assertEqual(result["pdf_failures"], 0)


if __name__ == "__main__":
    unittest.main()
