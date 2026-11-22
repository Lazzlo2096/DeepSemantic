from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import os
import sys
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))
import papers


class PaperChunksAuthTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(papers.router)
        self.client = TestClient(app)
        self.secret = patch.dict(os.environ, {"AUTH_PROXY_SECRET": "test-proxy-secret"})
        self.secret.start()

    def tearDown(self):
        self.secret.stop()
        self.client.close()

    @staticmethod
    def headers(role):
        return {
            "X-Auth-Proxy-Secret": "test-proxy-secret",
            "X-Authenticated-User": "00000000-0000-0000-0000-000000000001",
            "X-Authenticated-Email": f"{role}@example.test",
            "X-Authenticated-Role": role,
        }

    def test_chunks_require_authentication(self):
        response = self.client.get("/papers/2611.00001v1/chunks")
        self.assertEqual(response.status_code, 401)

    def test_chunks_reject_regular_user(self):
        response = self.client.get("/papers/2611.00001v1/chunks", headers=self.headers("user"))
        self.assertEqual(response.status_code, 403)

    def test_chunks_are_available_to_admin(self):
        rows = [{"chunk_id": 7, "chunk_index": 0, "text": "Debug chunk"}]
        session = SimpleNamespace(execute=self._execute(rows))

        @asynccontextmanager
        async def database_session(request):
            yield session

        with patch.object(papers, "database_session", database_session):
            response = self.client.get("/papers/2611.00001v1/chunks", headers=self.headers("admin"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), rows)

    @staticmethod
    def _execute(rows):
        async def execute(*args, **kwargs):
            return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))

        return execute


if __name__ == "__main__":
    unittest.main()
