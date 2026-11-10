from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import JSON, bindparam, text

from database import database_session


class RagToolCall(BaseModel):
    position: int
    tool_name: str
    arguments: dict | str
    result: dict
    duration_ms: int


class RagRunRecord(BaseModel):
    run_id: UUID
    query: str
    status: Literal["running", "answered", "insufficient_evidence", "failed"]
    started_at: datetime
    finished_at: datetime | None
    answer: dict | None
    error: str | None
    tool_calls: list[RagToolCall]


async def start_run(request: Request, query: str) -> UUID:
    run_id = uuid4()
    async with database_session(request) as session:
        await session.execute(text("""
            INSERT INTO rag_runs (id, query, status) VALUES (:id, :query, 'running')
        """), {"id": run_id, "query": query})
    return run_id


async def save_tool_call(request: Request, run_id: UUID, call: RagToolCall) -> None:
    async with database_session(request) as session:
        await session.execute(text("""
            INSERT INTO rag_tool_calls (run_id, position, tool_name, arguments, result, duration_ms)
            VALUES (:run_id, :position, :tool_name, :arguments, :result, :duration_ms)
        """).bindparams(bindparam("arguments", type_=JSON), bindparam("result", type_=JSON)), {
            "run_id": run_id, **call.model_dump(),
        })


async def finish_run(request: Request, run_id: UUID, status: str, *,
                     answer: dict | None = None, error: str | None = None) -> None:
    async with database_session(request) as session:
        await session.execute(text("""
            UPDATE rag_runs SET status = :status, finished_at = now(), answer = :answer, error = :error
            WHERE id = :id
        """).bindparams(bindparam("answer", type_=JSON(none_as_null=True))), {
            "id": run_id, "status": status, "answer": answer, "error": error,
        })


async def load_run(request: Request, run_id: UUID) -> RagRunRecord:
    async with database_session(request) as session:
        run = (await session.execute(text("""
            SELECT id AS run_id, query, status, started_at, finished_at, answer, error
            FROM rag_runs WHERE id = :id
        """), {"id": run_id})).mappings().one_or_none()
        if run is None:
            raise HTTPException(404, "RAG run not found.")
        calls = (await session.execute(text("""
            SELECT position, tool_name, arguments, result, duration_ms
            FROM rag_tool_calls WHERE run_id = :id ORDER BY position
        """), {"id": run_id})).mappings().all()
    return RagRunRecord(**run, tool_calls=calls)
