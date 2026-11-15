"""Exercise provider selection through the same interface used by API and workers."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from embeddings import create_embeddings
from embeddings_config import EmbeddingSettings
from embedding_schemas import EmbeddingRequest


class EmbeddingProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_provider_uses_its_url_and_authentication(self):
        for provider, url, authorization in [
            ("deepkaravan", "http://deepkaravan:8000/api/v1/embeddings", None),
            ("openrouter", "https://openrouter.ai/api/v1/embeddings", "Bearer test-key"),
        ]:
            with self.subTest(provider=provider):
                requests = []

                def handler(request):
                    requests.append(request)
                    self.assertEqual(str(request.url), url)
                    self.assertEqual(request.headers.get("authorization"), authorization)
                    self.assertEqual(json.loads(request.content), {
                        "model": "baai/bge-m3", "input": ["hello"], "encoding_format": "float",
                    })
                    return httpx.Response(200, json={"data": [
                        {"index": 0, "embedding": [3, 4] + [0] * 1022},
                    ]})

                with patch.dict("os.environ", {
                    "EMBEDDING_PROVIDER": provider, "OPENROUTER_API_KEY": "test-key",
                }, clear=True):
                    async with create_embeddings(transport=httpx.MockTransport(handler)) as embeddings:
                        result = await embeddings.embed(EmbeddingRequest(texts=["hello"]))
                        self.assertEqual(result["embeddings"][0][:2], [0.6, 0.8])
                        self.assertEqual(len(requests), 1)
                        self.assertEqual(embeddings.client.timeout.read, 60)
                    self.assertTrue(embeddings.client.is_closed)

    async def test_openrouter_without_key_never_sends_request(self):
        def handler(request):
            self.fail("Missing credentials must be rejected before sending a request")

        with patch.dict("os.environ", {"EMBEDDING_PROVIDER": "openrouter"}, clear=True):
            async with create_embeddings(transport=httpx.MockTransport(handler)) as embeddings:
                with self.assertRaises(HTTPException) as raised:
                    await embeddings.embed(EmbeddingRequest(texts=["hello"]))
                self.assertEqual(raised.exception.status_code, 503)

    async def test_custom_url_and_timeout(self):
        def handler(request):
            self.assertEqual(str(request.url), "http://localhost:9009/api/v1/embeddings")
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(503)

        with patch.dict("os.environ", {
            "EMBEDDING_BASE_URL": "http://localhost:9009/api/v1", "EMBEDDING_TIMEOUT": "120",
            "OPENROUTER_API_KEY": "must-not-be-sent",
        }, clear=True):
            async with create_embeddings(transport=httpx.MockTransport(handler)) as embeddings:
                self.assertEqual(embeddings.client.timeout.read, 120)
                with self.assertRaises(HTTPException) as raised:
                    await embeddings.embed(EmbeddingRequest(texts=["hello"]))
                self.assertEqual(raised.exception.status_code, 503)

    async def test_invalid_configuration_is_rejected(self):
        for env in [
            {"EMBEDDING_PROVIDER": "typo"},
            *({"EMBEDDING_TIMEOUT": value} for value in ["0", "-1", "nan", "inf", "invalid"]),
        ]:
            with self.subTest(env=env), patch.dict("os.environ", env, clear=True):
                with self.assertRaises(ValueError):
                    EmbeddingSettings.from_env()
