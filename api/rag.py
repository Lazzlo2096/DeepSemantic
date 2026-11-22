import asyncio
import json
import logging
import re
from dataclasses import dataclass
from time import perf_counter
from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from langchain.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openrouter import ChatOpenRouter
from langgraph.errors import GraphRecursionError, NodeCancelledError
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.runtime import Runtime
from openrouter.errors import NoResponseError, OpenRouterError, ResponseValidationError
from pydantic import UUID4, BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from typing_extensions import TypedDict

from agents.prompts import ANSWER_INSTRUCTIONS, ANSWER_JSON_FORMAT, PROMPT_VERSION, RESEARCH_INSTRUCTIONS
from agents.tools import TOOLS, QueryText, create_tools
from auth import CurrentUser
from chunks import PaperChunkSource, require_zero_chunk_overlap
from llm_chat import ChatResponseError
from rag_history import (
    RagModelTurn, RagRunRecord, RagToolCall, finish_run, load_run, save_model_turn, save_tool_call, start_run,
)


router = APIRouter(prefix="/rag", tags=["rag"])
logger = logging.getLogger(__name__)
# Bound research loops explicitly instead of relying on LangGraph's default.
RAG_RECURSION_LIMIT = 1000
REASONING_TAG = re.compile(r"<(think|thinking|reasoning)\b[^>]*>.*?(?:</\1\s*>|$)", re.IGNORECASE | re.DOTALL)


class RagQuestion(BaseModel):
    query: QueryText
    run_id: UUID4 | None = None


class AnswerPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    source_ids: list[int] = Field(min_length=1)


class GeneratedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    points: list[AnswerPoint]
    source_explanation: str


class RagSource(BaseModel):
    id: int
    arxiv_id: str
    paper_title: str | None
    chunk_index: int | None
    source_kind: PaperChunkSource
    source_start: int
    source_end: int
    text: str


class RagAnswer(GeneratedAnswer):
    run_id: UUID
    status: Literal["answered", "insufficient_evidence"]
    sources: list[RagSource]


def visible_model_content(message: AIMessage) -> str | None:
    return REASONING_TAG.sub("", message.text).replace("\x00", "").strip() or None


def reasoning_available(message: AIMessage) -> bool:
    return any(message.additional_kwargs.get(key) for key in ("reasoning_content", "reasoning_details", "thinking"))


def visible_reasoning(message: AIMessage) -> list[dict]:
    parts = []
    details = message.additional_kwargs.get("reasoning_details")
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict):
                continue
            kind = detail.get("type")
            if kind == "reasoning.text":
                content = detail.get("text")
            elif kind == "reasoning.summary":
                content = detail.get("summary")
            else:
                continue
            if isinstance(content, str) and content.strip():
                parts.append({"kind": kind, "text": content})
    for field in ("reasoning_content", "thinking"):
        content = message.additional_kwargs.get(field)
        texts = [part["text"] for part in parts if part["kind"] == "reasoning.text"]
        if isinstance(content, str) and content.strip() and content not in texts and content != "\n".join(texts):
            parts.append({"kind": "reasoning.text", "text": content})
    return parts


def parse_generated_answer(content: str) -> GeneratedAnswer:
    """Parse model JSON without relying on provider-side structured output."""
    content = content.strip()
    if content.startswith("```") and content.endswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE).strip()
    return GeneratedAnswer.model_validate_json(content)


def model_tool_calls(message: AIMessage) -> list[dict]:
    return [*message.tool_calls, *message.invalid_tool_calls]


def check_model_response(message: AIMessage, *, research: bool = False) -> None:
    allowed_reasons = ("stop", "tool_calls") if research else ("stop",)
    if message.response_metadata.get("finish_reason") not in allowed_reasons:
        raise ChatResponseError(f"finish_reason={message.response_metadata.get('finish_reason')!r}")


class RagState(TypedDict):
    question: str
    messages: Annotated[list[AnyMessage], add_messages]
    sources: dict[tuple, RagSource]
    search_steps: list[dict]
    versions: dict
    position: int
    answer: RagAnswer


@dataclass(frozen=True)
class RagContext:
    request: Request
    run_id: UUID
    model: ChatOpenRouter
    record_history: bool = True


async def research_node(state: RagState, runtime: Runtime[RagContext]) -> dict:
    context = runtime.context
    assistant = await context.model.bind_tools(TOOLS).ainvoke(state["messages"])
    check_model_response(assistant, research=True)
    calls = model_tool_calls(assistant)
    position = state["position"] + 1
    if context.record_history:
        await save_model_turn(context.request, context.run_id, RagModelTurn(
            position=position, phase="research", content=visible_model_content(assistant),
            requested_tools=[{"name": call["name"], "arguments": call["args"]} for call in calls],
            reasoning=visible_reasoning(assistant), reasoning_available=reasoning_available(assistant),
        ))
    return {"messages": [assistant], "position": position}


async def tools_node(state: RagState, runtime: Runtime[RagContext]) -> dict:
    context = runtime.context
    sources = dict(state["sources"])
    versions = dict(state["versions"])
    search_steps = list(state["search_steps"])
    tools = create_tools(context.request, versions)
    messages = []
    # Keep execution sequential: source IDs and the UI trace follow call order.
    for call in model_tool_calls(state["messages"][-1]):
        name, arguments = call["name"], call["args"]
        if not isinstance(name, str) or not isinstance(arguments, (str, dict)) or not call.get("id"):
            raise ChatResponseError("Malformed tool call")
        started = perf_counter()
        result = {"error": "Tool execution did not complete."}
        try:
            if name not in tools:
                raise HTTPException(400, "Unknown tool.")
            values = json.loads(arguments) if isinstance(arguments, str) else arguments
            matches = await tools[name].ainvoke(values)
            items = []
            for match in matches:
                key = (match["document_id"], match["source_kind"], match["source_start"], match["source_end"], match["text"])
                source = sources.get(key)
                source_id = source.id if source else len(sources) + 1
                item = {"source_id": source_id, **match}
                if source is None:
                    sources[key] = RagSource(id=source_id, arxiv_id=match["document_id"], **match)
                items.append(item)
            result = items[0] if name == "read_document" and items else {"matches": items}
        except (ValidationError, json.JSONDecodeError):
            result = {"error": "Invalid arguments. Provide a nonempty query or a valid document range."}
        except HTTPException as error:
            result = {"error": error.detail}
        finally:
            if context.record_history:
                await save_tool_call(context.request, context.run_id, RagToolCall(
                    position=len(search_steps) + 1, tool_name=name,
                    arguments=arguments, result=result, duration_ms=round((perf_counter() - started) * 1000),
                ))
        returned = result.get("matches", [result] if "source_id" in result else [])
        search_steps.append({
            "tool_name": name, "source_ids": [item["source_id"] for item in returned], "error": "error" in result,
        })
        messages.append(ToolMessage(content=json.dumps(result, ensure_ascii=False), tool_call_id=call["id"]))
    return {"messages": messages, "sources": sources, "search_steps": search_steps, "versions": versions}


def route_research(state: RagState) -> Literal["tools", "answer", "insufficient_evidence"]:
    if model_tool_calls(state["messages"][-1]):
        return "tools"
    return "answer" if state["sources"] else "insufficient_evidence"


async def answer_node(state: RagState, runtime: Runtime[RagContext]) -> dict:
    context = runtime.context
    material = json.dumps({
        "question": state["question"], "search_steps": state["search_steps"],
        "sources": [source.model_dump() for source in state["sources"].values()],
    }, ensure_ascii=False)
    # Nemotron supports reasoning and tool calls, but its available OpenRouter
    # endpoints do not support response_format=json_schema. Ask for the same
    # contract in plain text and validate it locally instead.
    assistant = await context.model.ainvoke([
        SystemMessage(content=ANSWER_INSTRUCTIONS + ANSWER_JSON_FORMAT), HumanMessage(content=material),
    ])
    check_model_response(assistant)
    content = visible_model_content(assistant)
    if context.record_history:
        await save_model_turn(context.request, context.run_id, RagModelTurn(
            position=state["position"] + 1, phase="answer", content=content, requested_tools=[],
            reasoning=visible_reasoning(assistant), reasoning_available=reasoning_available(assistant),
        ))
    if content is None:
        raise ChatResponseError("Empty response")
    # Validate the full JSON, including its closing delimiter. LangChain's parser
    # can recover partial JSON; the API must reject incomplete provider output.
    generated = parse_generated_answer(content)
    cited_ids = {source_id for point in generated.points for source_id in point.source_ids}
    sources_by_id = {source.id: source for source in state["sources"].values()}
    if cited_ids - sources_by_id.keys():
        raise ValueError("Unknown source ID")
    return {"answer": RagAnswer(
        run_id=context.run_id,
        status="answered" if generated.points else "insufficient_evidence",
        points=generated.points, sources=[sources_by_id[id] for id in sorted(cited_ids)],
        source_explanation=generated.source_explanation if generated.points else "",
    )}


def insufficient_evidence_node(state: RagState, runtime: Runtime[RagContext]) -> dict:
    return {"answer": RagAnswer(
        run_id=runtime.context.run_id, status="insufficient_evidence", points=[], sources=[], source_explanation="",
    )}


RAG_NODES = {
    "research": research_node,
    "tools": tools_node,
    "answer": answer_node,
    "insufficient_evidence": insufficient_evidence_node,
}


def add_rag_edges(graph: StateGraph, entry: str = START) -> StateGraph:
    graph.add_edge(entry, "research")
    graph.add_conditional_edges("research", route_research)
    graph.add_edge("tools", "research")
    graph.add_edge("answer", END)
    graph.add_edge("insufficient_evidence", END)
    return graph


def build_rag_graph():
    graph = StateGraph(RagState, context_schema=RagContext)
    for name, node in RAG_NODES.items():
        graph.add_node(name, node)
    add_rag_edges(graph)
    return graph.compile()


RAG_GRAPH = build_rag_graph()


async def generate_answer(payload: RagQuestion, request: Request, run_id: UUID) -> RagAnswer:
    state = request.app.state
    chat = state.chat_settings

    try:
        result = await RAG_GRAPH.ainvoke(
            {
                "question": payload.query,
                "messages": [SystemMessage(content=RESEARCH_INSTRUCTIONS), HumanMessage(content=payload.query)],
                "sources": {}, "search_steps": [], "versions": {}, "position": 0,
            },
            config={"recursion_limit": RAG_RECURSION_LIMIT},
            context=RagContext(request=request, run_id=run_id, model=state.rag_model),
        )
        return result["answer"]
    except NodeCancelledError as error:
        raise asyncio.CancelledError("RAG request interrupted.") from error
    except GraphRecursionError as error:
        raise HTTPException(502, "RAG search exceeded its step limit.") from error
    except (httpx.TimeoutException, TimeoutError) as error:
        raise HTTPException(504, f"{chat.provider} response timed out.") from error
    except ResponseValidationError as error:
        raise HTTPException(502, f"{chat.provider} returned an incomplete answer or invalid citations.") from error
    except (httpx.HTTPStatusError, OpenRouterError) as error:
        status = error.status_code if isinstance(error, OpenRouterError) else error.response.status_code
        body = error.body if isinstance(error, OpenRouterError) else error.response.text
        detail = body.strip() or str(error)
        # Keep the upstream diagnostic in the API response.  In particular,
        # OpenRouter uses the body to explain routing failures (for example,
        # when no provider supports the requested response format).
        detail = detail[:4000]
        logger.warning("%s returned HTTP %d for RAG", chat.provider, status)
        raise HTTPException(
            503 if status in (404, 503) else 502,
            f"{chat.provider} returned HTTP {status}: {detail}",
        ) from error
    except (httpx.RequestError, NoResponseError) as error:
        raise HTTPException(503, f"{chat.provider} is unavailable.") from error
    except ValueError as error:
        raise HTTPException(502, f"{chat.provider} returned an incomplete answer or invalid citations.") from error


@router.post("/answer", response_model=RagAnswer)
async def answer_question(payload: RagQuestion, request: Request, user: CurrentUser):
    require_zero_chunk_overlap()
    chat = request.app.state.chat_settings
    if not chat.configured:
        raise HTTPException(503, "OPENROUTER_API_KEY is required.")

    reasoning_effort = chat.reasoning_effort or "disabled"
    run_id = await start_run(
        request, payload.query, payload.run_id,
        user_id=user.id, model=chat.model, reasoning_effort=reasoning_effort, prompt_version=PROMPT_VERSION,
    )
    headers = {"X-RAG-Run-Id": str(run_id)}
    try:
        answer = await generate_answer(payload, request, run_id)
    except asyncio.CancelledError:
        await finish_run(request, run_id, "failed", error="RAG request interrupted.")
        raise
    except HTTPException as error:
        await finish_run(request, run_id, "failed", error=str(error.detail))
        raise HTTPException(error.status_code, error.detail, {**(error.headers or {}), **headers}) from error
    except Exception as error:
        logger.exception("RAG run %s failed", run_id)
        message = "RAG request failed."
        await finish_run(request, run_id, "failed", error=message)
        raise HTTPException(500, message, headers) from error
    await finish_run(request, run_id, answer.status, answer=answer.model_dump(mode="json"))
    return answer


@router.get("/runs/{run_id}", response_model=RagRunRecord)
async def get_run(request: Request, response: Response, run_id: UUID, user: CurrentUser):
    response.headers["Cache-Control"] = "no-store"
    return await load_run(request, run_id, user.id)
