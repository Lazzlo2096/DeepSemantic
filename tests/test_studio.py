"""Studio adapter contracts: schemas, checkpoints, replay, and isolated resources."""

from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from langchain.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langsmith-studio"))

from llm_chat import ChatSettings
from rag import RagContext
from rag_model import create_rag_model
import studio
from test_rag import answer_content, chunk, completion, tool_call


class StudioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.responses = []
        self.requests = []

        def handler(request):
            self.requests.append(json.loads(request.content))
            return httpx.Response(200, json=self.responses.pop(0))

        client = await self.enterAsyncContext(httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None))
        model = create_rag_model(ChatSettings("openrouter", "test", "https://openrouter.ai/api/v1/", "test-key"), client)
        self.context = RagContext(request=SimpleNamespace(), run_id=uuid4(), model=model, record_history=False)
        self.vectors = self.enterContext(patch("agents.tools.find_chunks", new_callable=AsyncMock, return_value=[chunk()]))
        self.model_history = self.enterContext(patch("rag.save_model_turn", new_callable=AsyncMock))
        self.tool_history = self.enterContext(patch("rag.save_tool_call", new_callable=AsyncMock))
        self.enterContext(patch.dict("os.environ", {"CHUNK_OVERLAP_PERCENT": "0"}))
        self.config = {"configurable": {"thread_id": "studio-test"}}
        self.graph = studio.build_studio_graph(self.context, checkpointer=InMemorySaver())

    def respond_to_question(self):
        self.responses.extend([
            completion(calls=[tool_call("search_vectors", {"query": "research"})]),
            completion("Done"), completion(answer_content()),
        ])

    async def test_schemas_and_graph_are_available_without_resources(self):
        with patch("studio.lifespan") as resources:
            async with studio.graph({}, SimpleNamespace(execution_runtime=None)) as graph:
                schema = graph.get_input_jsonschema()
                self.assertEqual(set(schema["properties"]), {"question"})
                graph.get_output_jsonschema()
                diagram = graph.get_graph().draw_mermaid()
                for name in ["prepare", "research", "tools", "answer", "insufficient_evidence"]:
                    self.assertIn(name, diagram)
                self.assertIsNone(graph.context_schema)
            resources.assert_not_called()

    async def test_browser_preflight_allows_studio_private_network_requests(self):
        from studio_http import app

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            headers = {
                "Origin": "https://smith.langchain.com", "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-api-key",
                "Access-Control-Request-Private-Network": "true",
            }
            response = await client.options("/threads", headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["access-control-allow-origin"], headers["Origin"])
            self.assertEqual(response.headers["access-control-allow-private-network"], "true")
            response = await client.options("/threads", headers={**headers, "Origin": "https://untrusted.example"})
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("access-control-allow-origin", response.headers)

    async def test_state_is_json_safe_and_restores_after_tools(self):
        self.respond_to_question()
        partial = await self.graph.ainvoke({"question": "  Explain research  "}, self.config, interrupt_after=["tools"])
        self.assertEqual(partial["question"], "Explain research")
        self.assertEqual(partial["sources"]["1"]["arxiv_id"], "2611.00001")
        json.dumps({key: value for key, value in partial.items() if key != "messages"})
        self.assertTrue(all(isinstance(source, dict) for source in partial["sources"].values()))
        self.assertEqual(len(self.requests), 1)

        result = await self.graph.ainvoke(None, self.config)
        self.assertEqual(result["answer"]["status"], "answered")
        self.assertEqual(result["answer"]["run_id"], str(self.context.run_id))
        self.assertEqual(result["answer"]["sources"][0]["id"], 1)
        self.model_history.assert_not_awaited()
        self.tool_history.assert_not_awaited()
        snapshot = await self.graph.aget_state(self.config)
        self.assertEqual(snapshot.next, ())
        self.assertIsInstance(snapshot.values["messages"][-1], AIMessage)
        self.assertEqual(snapshot.values["sources"]["1"]["text"], chunk().text)

    async def test_new_question_resets_previous_thread_state(self):
        for question in ["First question", "Second question"]:
            self.respond_to_question()
            result = await self.graph.ainvoke({"question": question}, self.config)
            self.assertEqual(result["question"], question)
            self.assertEqual(result["position"], 2)
            self.assertEqual(len(result["search_steps"]), 1)
            self.assertEqual(len(result["sources"]), 1)
            self.assertEqual(len(self.requests[-3]["messages"]), 2)
            self.assertEqual(self.requests[-3]["messages"][1]["content"], question)

    async def test_empty_question_fails_before_provider_call(self):
        with self.assertRaises(ValidationError):
            await self.graph.ainvoke({"question": "  "}, self.config)
        self.assertEqual(self.requests, [])

    async def test_no_sources_produces_serializable_empty_answer(self):
        self.responses = [completion("Done")]
        result = await self.graph.ainvoke({"question": "Missing topic"}, self.config)
        self.assertEqual(result["answer"]["status"], "insufficient_evidence")
        self.assertEqual(result["answer"]["points"], [])
        self.assertIsInstance(result["answer"], dict)

    async def test_graph_factory_closes_resources_and_disables_website_journal(self):
        events = []
        run_id = uuid4()

        @asynccontextmanager
        async def resources(app):
            events.append("open")
            app.state.rag_model = self.context.model
            try:
                yield
            finally:
                events.append("close")

        self.responses = [completion("Done")]
        with patch("studio.lifespan", resources):
            async with studio.graph({"configurable": {"run_id": str(run_id)}},
                                    SimpleNamespace(execution_runtime=object())) as graph:
                result = await graph.ainvoke({"question": "Question"})
                self.assertEqual(result["answer"]["run_id"], str(run_id))
                self.assertEqual(events, ["open"])
        self.assertEqual(events, ["open", "close"])
        self.model_history.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
