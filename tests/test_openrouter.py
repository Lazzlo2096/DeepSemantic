"""Offline API contract checks: python -m unittest discover -s tests -v."""

import importlib.util
import json
import math
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from embedding_schemas import EMBEDDING_OPENROUTER_MODEL, EmbeddingRequest
from llm_chat import ChatResponseError, ChatSettings
from openrouter_embeddings import OpenRouterEmbeddings


class ApiLifecycleTests(unittest.TestCase):
    def test_api_starts_without_embedding_worker(self):
        from main import app

        with patch.dict("os.environ", {
            "OPENROUTER_API_KEY": "", "RUSTFS_ENDPOINT": "",
            "POSTGRES_DB": "test", "POSTGRES_USER": "test", "POSTGRES_PASSWORD": "test",
        }):
            with TestClient(app) as client:
                self.assertEqual(client.get("/health/live").status_code, 200)
                self.assertEqual(client.get("/health").status_code, 503)
                self.assertEqual(client.post("/embeddings", json={"texts": ["hello"]}).status_code, 503)
                self.assertEqual(client.post("/embeddings", json={"texts": []}).status_code, 422)


class OpenRouterTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler):
        return httpx.AsyncClient(
            base_url="https://openrouter.ai/api/v1/",
            headers={"Authorization": "Bearer test-key"},
            transport=httpx.MockTransport(handler),
        )

    async def test_embeddings_preserve_order_and_normalize(self):
        def handler(request):
            self.assertEqual(str(request.url), "https://openrouter.ai/api/v1/embeddings")
            self.assertEqual(request.headers["Authorization"], "Bearer test-key")
            self.assertEqual(json.loads(request.content)["input"], ["first", "second"])
            dimension = 1024 if EMBEDDING_OPENROUTER_MODEL == "baai/bge-m3" else 3
            return httpx.Response(200, json={"data": [
                {"index": 1, "embedding": [0, 2] + [0] * (dimension - 2)},
                {"index": 0, "embedding": [3, 4] + [0] * (dimension - 2)},
            ]})

        async with self.client(handler) as client:
            result = await OpenRouterEmbeddings(client, "test-key").embed(
                EmbeddingRequest(texts=["first", "second"]),
            )
        self.assertEqual(result["revision"], "openrouter")
        self.assertEqual(result["embeddings"][0][:2], [0.6, 0.8])
        self.assertAlmostEqual(math.hypot(*result["embeddings"][1]), 1)

    async def test_embedding_failures_are_http_errors(self):
        scenarios = [
            (400, {}, 422), (401, {}, 503), (429, {}, 503), (500, {}, 503),
            (200, {"data": []}, 502),
            (200, {"data": [{"index": 3, "embedding": [1]}]}, 502),
            (200, {"data": [{"index": 0, "embedding": [0] * 1024}]}, 502),
        ]
        for status, body, expected in scenarios:
            with self.subTest(status=status, body=body):
                async with self.client(lambda _: httpx.Response(status, json=body)) as client:
                    with self.assertRaises(HTTPException) as raised:
                        await OpenRouterEmbeddings(client, "test-key").embed(EmbeddingRequest(texts=["text"]))
                self.assertEqual(raised.exception.status_code, expected)

    async def test_missing_key_never_calls_provider(self):
        def handler(_):
            self.fail("Unconfigured embeddings must not send requests")

        async with self.client(handler) as client:
            with self.assertRaises(HTTPException) as raised:
                await OpenRouterEmbeddings(client, "").embed(EmbeddingRequest(texts=["text"]))
        self.assertEqual(raised.exception.status_code, 503)

    async def test_embedding_transport_failures(self):
        for error_type, expected in [(httpx.ReadTimeout, 504), (httpx.ConnectError, 503)]:
            def handler(request):
                raise error_type("unavailable", request=request)

            async with self.client(handler) as client:
                with self.assertRaises(HTTPException) as raised:
                    await OpenRouterEmbeddings(client, "test-key").embed(EmbeddingRequest(texts=["text"]))
            self.assertEqual(raised.exception.status_code, expected)

    async def test_chat_tools_and_structured_output(self):
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}):
            chat = ChatSettings.from_env(openrouter_model_env="RAG_OPENROUTER_MODEL")

        def handler(request):
            self.assertEqual(request.url.path, "/api/v1/chat/completions")
            payload = json.loads(request.content)
            self.assertTrue(payload["provider"]["require_parameters"])
            self.assertEqual(payload["response_format"]["type"], "json_schema")
            self.assertEqual(payload["tools"][0]["type"], "function")
            return httpx.Response(200, json={"choices": [{
                "finish_reason": "tool_calls", "message": {"role": "assistant", "tool_calls": []},
            }]})

        async with self.client(handler) as client:
            result = await chat.turn(client, [], max_tokens=None,
                                     tools=[{"type": "function"}], schema={"type": "object"})
        self.assertEqual(result["role"], "assistant")

    async def test_incomplete_chat_is_rejected(self):
        chat = ChatSettings("openrouter", "test-model", "https://openrouter.ai/api/v1/", "test-key")
        async with self.client(lambda _: httpx.Response(200, json={"choices": [{
            "finish_reason": "length", "message": {"content": "partial"},
        }]})) as client:
            with self.assertRaises(ChatResponseError):
                await chat.complete(client, [], max_tokens=None)

    async def test_summarization_uses_openrouter(self):
        spec = importlib.util.spec_from_file_location("summary_worker", ROOT / "summary-worker/main.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        progress_events = []

        async def progress(done, total):
            progress_events.append((done, total))

        def handler(request):
            self.assertIn("source article", json.loads(request.content)["messages"][0]["content"])
            return httpx.Response(200, json={"choices": [{
                "finish_reason": "stop", "message": {"content": "summary"},
            }]})

        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}):
            chat = ChatSettings.from_env(openrouter_model_env="SUMMARY_OPENROUTER_MODEL")
        async with self.client(handler) as client:
            self.assertEqual(await module.Summarizer(client, chat).summarize("source article", progress), "summary")
        self.assertEqual(progress_events, [(0, 1), (1, 1)])


if __name__ == "__main__":
    unittest.main()
