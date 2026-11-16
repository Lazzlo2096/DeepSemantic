"""Expose the existing RAG nodes to Agent Server without checkpointing clients or credentials."""

from contextlib import asynccontextmanager
import inspect
import json
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from langchain.messages import AnyMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import START, StateGraph, add_messages
from langgraph.runtime import Runtime
from langgraph.types import Overwrite
from langgraph_sdk.runtime import ServerRuntime
from typing_extensions import TypedDict

from agents.prompts import RESEARCH_INSTRUCTIONS
from agents.tools import QueryText
from main import lifespan
from rag import (
    RAG_NODES, RAG_RECURSION_LIMIT,
    RagContext, RagQuestion, RagSource, add_rag_edges,
)


class StudioInput(TypedDict):
    question: QueryText


class StudioState(TypedDict):
    question: str
    messages: Annotated[list[AnyMessage], add_messages]
    sources: dict[str, dict]
    search_steps: list[dict]
    versions: dict[str, str | None]
    position: int
    answer: dict | None


def prepare_question(state: StudioState) -> dict:
    question = RagQuestion(query=state["question"]).query
    return {
        "question": question,
        "messages": Overwrite([SystemMessage(content=RESEARCH_INSTRUCTIONS), HumanMessage(content=question)]),
        "sources": {}, "search_steps": [], "versions": {}, "position": 0, "answer": None,
    }


def decode_state(state: StudioState) -> dict:
    sources = [RagSource.model_validate(source) for source in state["sources"].values()]
    return {
        **state,
        "sources": {
            (source.arxiv_id, source.source_kind, source.source_start, source.source_end, source.text): source
            for source in sources
        },
        "versions": {tuple(json.loads(key)): version for key, version in state["versions"].items()},
    }


def encode_update(update: dict) -> dict:
    result = dict(update)
    if "sources" in result:
        result["sources"] = {str(source.id): source.model_dump(mode="json") for source in result["sources"].values()}
    if "versions" in result:
        result["versions"] = {json.dumps(key): version for key, version in result["versions"].items()}
    if "answer" in result:
        result["answer"] = result["answer"].model_dump(mode="json")
    return result


def build_studio_graph(context: RagContext | None = None, *, checkpointer=None):
    def bind(node):
        async def execute(state: StudioState, runtime: Runtime) -> dict:
            if context is None:
                raise RuntimeError("RAG execution requires an Agent Server runtime.")
            update = node(decode_state(state), runtime.override(context=context))
            if inspect.isawaitable(update):
                update = await update
            return encode_update(update)

        return execute

    graph = StateGraph(StudioState, input_schema=StudioInput)
    graph.add_node("prepare", prepare_question)
    for name, node in RAG_NODES.items():
        graph.add_node(name, bind(node))
    graph.add_edge(START, "prepare")
    add_rag_edges(graph, entry="prepare")
    return graph.compile(name="DeepSemantic RAG", checkpointer=checkpointer).with_config(recursion_limit=RAG_RECURSION_LIMIT)


@asynccontextmanager
async def graph(config: RunnableConfig, runtime: ServerRuntime):
    # Studio requests schemas before executing anything. Viewing the graph must
    # work without OpenRouter credentials, PostgreSQL, Qdrant, or RustFS.
    if runtime.execution_runtime is None:
        yield build_studio_graph()
        return

    app = FastAPI()
    async with lifespan(app):
        if app.state.rag_model is None:
            raise ValueError("OPENROUTER_API_KEY is required to run RAG from Studio.")
        request = Request({"type": "http", "app": app, "method": "POST", "path": "/studio"})
        run_id = config.get("configurable", {}).get("run_id") or config.get("run_id") or uuid4()
        # Agent Server owns Studio's checkpoints/history. Replaying a node must
        # not insert duplicate positions into the website's append-only journal.
        context = RagContext(request=request, run_id=UUID(str(run_id)), model=app.state.rag_model, record_history=False)
        yield build_studio_graph(context)
