"""Exercise the real LangChain/LangGraph pipeline with offline OpenRouter responses."""

import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from chunks import ChunkMatch, ChunkSearchResult
from llm_chat import ChatSettings
import rag
from rag_model import create_rag_model


def completion(content=None, *, calls=None, finish_reason=None, **fields):
    message = {"role": "assistant", "content": content, **fields}
    if calls is not None:
        message["tool_calls"] = calls
    return {
        "id": "test-completion", "object": "chat.completion", "created": 1,
        "model": "test-model", "system_fingerprint": None,
        "choices": [{
            "index": 0, "finish_reason": finish_reason or ("tool_calls" if calls else "stop"),
            "message": message,
        }],
    }


def tool_call(name, arguments, id="call-1"):
    return {"id": id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments) if isinstance(arguments, dict) else arguments,
    }}


def answer_content(ids=(1,), **fields):
    return json.dumps({
        "points": [{"text": "Grounded answer", "source_ids": list(ids)}], "source_explanation": "", **fields,
    })


def chunk(**fields):
    return ChunkSearchResult(**{
        "chunk_id": 42, "text": "The measured result.", "arxiv_id": "2611.00001",
        "paper_title": "Research paper", "chunk_index": 0, "source_kind": "article",
        "source_start": 0, "source_end": 20, "source_version": "version-1", "score": 0.9, **fields,
    })


class RagPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.responses = []
        self.requests = []
        self.model_turns = []
        self.tool_calls = []
        self.run_id = uuid4()

        def handler(request):
            self.requests.append(json.loads(request.content))
            self.assertEqual(str(request.url), "https://openrouter.ai/api/v1/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer test-key")
            self.assertTrue(all(value is None for value in request.extensions["timeout"].values()))
            response = self.responses.pop(0)
            if callable(response):
                response = response(json.loads(request.content))
            if isinstance(response, Exception):
                raise response
            return response if isinstance(response, httpx.Response) else httpx.Response(200, json=response)

        client = await self.enterAsyncContext(httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None))
        self.app = FastAPI()
        self.app.include_router(rag.router)
        settings = ChatSettings(
            "openrouter", "test-model", "https://openrouter.ai/api/v1/", "test-key",
            provider_order=("first-provider", "second-provider"),
        )
        self.app.state.chat_settings = settings
        self.app.state.rag_model = create_rag_model(settings, client)
        self.api = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
        ))
        self.start_run = self.enterContext(patch("rag.start_run", new_callable=AsyncMock, return_value=self.run_id))
        self.finish_run = self.enterContext(patch("rag.finish_run", new_callable=AsyncMock))
        self.enterContext(patch("rag.save_model_turn", new_callable=AsyncMock,
                                side_effect=lambda request, run_id, turn: self.model_turns.append(turn)))
        self.enterContext(patch("rag.save_tool_call", new_callable=AsyncMock,
                                side_effect=lambda request, run_id, call: self.tool_calls.append(call)))
        self.vectors = self.enterContext(patch("rag.find_chunks", new_callable=AsyncMock, return_value=[chunk()]))
        self.text = self.enterContext(patch("rag.find_text_chunks", new_callable=AsyncMock, return_value=[]))
        self.read = self.enterContext(patch("rag.read_document", new_callable=AsyncMock, return_value={
            "document_id": "2611.00001", "source_kind": "article", "source_start": 20, "source_end": 40,
            "text": "More context.", "paper_title": "Research paper", "chunk_index": None,
        }))
        self.enterContext(patch.dict("os.environ", {"CHUNK_OVERLAP_PERCENT": "0"}))

    async def ask(self, query="Explain the research"):
        return await self.api.post("/rag/answer", json={"query": query})

    def search_then_answer(self, content=None, **fields):
        self.responses.extend([
            completion(calls=[tool_call("search_vectors", {"query": "research"})]),
            completion("Done"),
            completion(content if content is not None else answer_content(), **fields),
        ])

    async def test_search_loop_then_separate_answer_preserves_trace_and_sources(self):
        reasoning = [{"type": "reasoning.text", "text": "Find primary results.", "format": "unknown", "index": 0}]
        self.responses = [
            completion(calls=[tool_call("search_vectors", {"query": "research"})],
                       reasoning="Find primary results.", reasoning_details=reasoning),
            completion(calls=[
                tool_call("search_text", {"query": "method"}, "call-2"),
                tool_call("read_document", {
                    "document_id": "2611.00001", "source_start": 20, "source_end": 40,
                }, "call-3"),
            ]),
            completion("Done"),
            completion(answer_content(ids=(1, 2))),
        ]
        self.text.return_value = [ChunkMatch(**chunk().model_dump(), source_version="version-1")]
        response = await self.ask()
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["run_id"], str(self.run_id))
        self.assertEqual(body["status"], "answered")
        self.assertEqual([source["id"] for source in body["sources"]], [1, 2])
        self.assertEqual([call.tool_name for call in self.tool_calls], ["search_vectors", "search_text", "read_document"])
        self.assertEqual([call.position for call in self.tool_calls], [1, 2, 3])
        self.assertEqual(self.tool_calls[1].result["matches"][0]["source_id"], 1)
        self.assertEqual(self.tool_calls[2].result["source_id"], 2)
        self.assertEqual([turn.phase for turn in self.model_turns], ["research"] * 3 + ["answer"])
        self.assertEqual([turn.position for turn in self.model_turns], [1, 2, 3, 4])
        self.assertEqual(self.model_turns[0].reasoning, [{"kind": "reasoning.text", "text": "Find primary results."}])
        self.assertTrue(self.model_turns[0].reasoning_available)

        first, second, third, final = self.requests
        self.assertEqual(first["messages"], [
            {"role": "system", "content": rag.RESEARCH_INSTRUCTIONS},
            {"role": "user", "content": "Explain the research"},
        ])
        self.assertEqual(second["messages"][2]["reasoning_details"], reasoning)
        self.assertEqual(second["messages"][-1]["tool_call_id"], "call-1")
        self.assertEqual([message["tool_call_id"] for message in third["messages"] if message["role"] == "tool"],
                         ["call-1", "call-2", "call-3"])
        self.assertEqual(len(final["messages"]), 2)
        self.assertEqual(final["messages"][0]["content"], rag.ANSWER_INSTRUCTIONS)
        self.assertNotIn("tools", final)
        material = json.loads(final["messages"][1]["content"])
        self.assertEqual(material["question"], "Explain the research")
        self.assertEqual(len(material["sources"]), 2)
        self.assertEqual([step["source_ids"] for step in material["search_steps"]], [[1], [1], [2]])
        self.assertEqual(final["response_format"]["type"], "json_schema")
        self.assertTrue(final["response_format"]["json_schema"]["strict"])
        for request in self.requests:
            self.assertEqual(request["model"], "test-model")
            self.assertEqual(request["reasoning"], {"effort": "none"})
            self.assertEqual(request["provider"], {
                "order": ["first-provider", "second-provider"], "require_parameters": True,
            })
        self.assertEqual(self.finish_run.call_args.args[2], "answered")
        self.assertEqual(self.start_run.call_args.kwargs["prompt_version"], rag.PROMPT_VERSION)

    async def test_no_sources_skips_answer_model(self):
        self.vectors.return_value = []
        self.responses = [
            completion(calls=[tool_call("search_vectors", {"query": "missing"})]), completion("Done"),
        ]
        response = await self.ask()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "insufficient_evidence")
        self.assertEqual(response.json()["points"], [])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.finish_run.call_args.args[2], "insufficient_evidence")

    async def test_invalid_tools_return_feedback_and_allow_recovery(self):
        self.responses = [completion(calls=[
            tool_call("search_vectors", '{"query":', "invalid-json"),
            tool_call("search_vectors", {"query": "research", "limit": "3"}, "invalid-type"),
            tool_call("read_document", {
                "document_id": "2611.00001", "source_start": 40, "source_end": 20,
            }, "invalid-range"),
            tool_call("unknown", {"query": "research"}, "unknown-tool"),
        ])]
        self.search_then_answer()
        response = await self.ask()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.tool_calls), 5)
        self.assertTrue(all("error" in call.result for call in self.tool_calls[:4]))
        self.vectors.assert_awaited_once()
        self.read.assert_not_awaited()
        feedback = [message for message in self.requests[1]["messages"] if message["role"] == "tool"]
        self.assertEqual(len(feedback), 4)
        self.assertTrue(all("error" in json.loads(message["content"]) for message in feedback))

    async def test_search_failure_allows_another_tool(self):
        self.vectors.side_effect = HTTPException(503, "Could not search Qdrant.")
        self.text.return_value = [ChunkMatch(**chunk(source_kind="summary").model_dump())]
        self.responses = [
            completion(calls=[tool_call("search_vectors", {"query": "research"})]),
            completion(calls=[tool_call("search_text", {"query": "method"})]),
            completion("Done"), completion(answer_content()),
        ]
        response = await self.ask()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.tool_calls[0].result, {"error": "Could not search Qdrant."})
        self.assertEqual(response.json()["sources"][0]["source_kind"], "summary")

    async def test_source_version_changes_do_not_mix_documents(self):
        self.vectors.side_effect = [[chunk()], [chunk(source_version="version-2", text="Changed text")]]
        self.responses = [
            completion(calls=[tool_call("search_vectors", {"query": "research"})]),
            completion(calls=[tool_call("search_vectors", {"query": "follow-up"})]),
            completion("Done"), completion(answer_content()),
        ]
        response = await self.ask()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.tool_calls[1].result, {"matches": []})
        self.assertEqual(response.json()["sources"][0]["text"], "The measured result.")

    async def test_invalid_or_incomplete_answers_fail_with_run_id(self):
        for content, finish_reason in [
            (answer_content(ids=(999,)), "stop"),
            (answer_content()[:-1], "stop"),
            (answer_content(), "length"),
            ("", "stop"),
        ]:
            with self.subTest(content=content, finish_reason=finish_reason):
                self.search_then_answer(content, finish_reason=finish_reason)
                response = await self.ask()
                self.assertEqual(response.status_code, 502, response.text)
                self.assertEqual(response.headers["X-RAG-Run-Id"], str(self.run_id))
                self.assertEqual(self.finish_run.call_args.args[2], "failed")

    async def test_provider_errors_are_mapped_without_retries(self):
        for error, expected in [
            (httpx.Response(404, json={"error": {"message": "Unavailable", "code": 404}}), 503),
            (httpx.Response(503, json={"error": {"message": "Unavailable", "code": 503}}), 503),
            (httpx.Response(429, json={"error": {"message": "Unavailable", "code": 429}}), 502),
            (httpx.Response(200, json={"choices": []}), 502),
            (httpx.ReadTimeout("Timed out"), 504),
            (httpx.ConnectError("Unavailable"), 503),
        ]:
            with self.subTest(error=error):
                self.responses.append(error)
                before = len(self.requests)
                response = await self.ask()
                self.assertEqual(response.status_code, expected, response.text)
                self.assertEqual(len(self.requests), before + 1)
                self.assertEqual(self.finish_run.call_args.args[2], "failed")

    async def test_successive_runs_have_isolated_state(self):
        for query in ["First question", "Second question"]:
            self.search_then_answer()
            response = await self.ask(query)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["sources"][0]["id"], 1)
            self.assertEqual(len(self.requests[-3]["messages"]), 2)
            material = json.loads(self.requests[-1]["messages"][1]["content"])
            self.assertEqual(material["question"], query)
            self.assertEqual(len(material["search_steps"]), 1)

    async def test_concurrent_runs_have_isolated_sources_and_messages(self):
        self.start_run.side_effect = lambda *args, **kwargs: uuid4()

        async def sources(request, query, limit, *, located_only):
            await asyncio.sleep(0)
            return [chunk(arxiv_id=query, text=f"Results for {query}")]

        self.vectors.side_effect = sources

        def respond(payload):
            if payload["messages"][0]["content"] == rag.ANSWER_INSTRUCTIONS:
                material = json.loads(payload["messages"][1]["content"])
                self.assertEqual(len(material["sources"]), 1)
                self.assertEqual(material["sources"][0]["arxiv_id"], material["question"])
                return completion(answer_content())
            question = payload["messages"][1]["content"]
            if len(payload["messages"]) == 2:
                return completion(calls=[tool_call("search_vectors", {"query": question})])
            self.assertEqual(len(payload["messages"]), 4)
            return completion("Done")

        self.responses = [respond] * 6
        responses = await asyncio.gather(self.ask("first-document"), self.ask("second-document"))
        for response, document_id in zip(responses, ["first-document", "second-document"]):
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["sources"][0]["id"], 1)
            self.assertEqual(response.json()["sources"][0]["arxiv_id"], document_id)
        self.assertNotEqual(responses[0].json()["run_id"], responses[1].json()["run_id"])

    async def test_only_cited_sources_are_returned(self):
        self.responses = [
            completion(calls=[
                tool_call("search_vectors", {"query": "research"}),
                tool_call("read_document", {
                    "document_id": "2611.00001", "source_start": 20, "source_end": 40,
                }, "call-2"),
            ]),
            completion("Done"), completion(answer_content(ids=(2,))),
        ]
        response = await self.ask()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([source["id"] for source in response.json()["sources"]], [2])
        material = json.loads(self.requests[-1]["messages"][1]["content"])
        self.assertEqual(len(material["sources"]), 2)

    async def test_model_can_decline_answer_despite_sources(self):
        self.search_then_answer(json.dumps({"points": [], "source_explanation": "Unsupported"}))
        response = await self.ask()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "insufficient_evidence")
        self.assertEqual(response.json()["sources"], [])
        self.assertEqual(response.json()["source_explanation"], "")

    async def test_configured_reasoning_is_sent(self):
        client = self.app.state.rag_model.client.sdk_configuration.async_client
        settings = ChatSettings("openrouter", "test-model", "https://openrouter.ai/api/v1/", "test-key",
                                reasoning_effort="high")
        self.app.state.chat_settings = settings
        self.app.state.rag_model = create_rag_model(settings, client)
        self.responses = [completion("Done")]
        response = await self.ask()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.requests[0]["reasoning"], {"effort": "high"})
        self.assertEqual(self.start_run.call_args.kwargs["reasoning_effort"], "high")

    async def test_recursion_limit_finishes_run_as_failed(self):
        self.responses = [completion(calls=[tool_call("search_vectors", {"query": "research"})])]
        with patch("rag.RAG_RECURSION_LIMIT", 1):
            response = await self.ask()
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(response.json()["detail"], "RAG search exceeded its step limit.")
        self.assertEqual(self.finish_run.call_args.args[2], "failed")

    async def test_cancellation_finishes_run_as_failed(self):
        self.vectors.side_effect = asyncio.CancelledError()
        self.responses = [completion(calls=[tool_call("search_vectors", {"query": "research"})])]
        with self.assertRaises(asyncio.CancelledError):
            await self.ask()
        self.assertEqual(self.finish_run.call_args.args[2], "failed")
        self.assertEqual(self.finish_run.call_args.kwargs["error"], "RAG request interrupted.")
        self.assertEqual(self.tool_calls[-1].result, {"error": "Tool execution did not complete."})


if __name__ == "__main__":
    unittest.main()
