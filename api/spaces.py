"""Space ownership and access control endpoints."""

from datetime import datetime
import os
from pathlib import PurePath
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from auth import CurrentUser, OptionalCurrentUser
from database import database_session


router = APIRouter(prefix="/spaces", tags=["spaces"])


class SpaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=10_000)
    visibility: Literal["private", "public"] = "private"


class SpaceMemberCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    permission: Literal["read", "write"] = "read"


class SpaceResponse(BaseModel):
    id: UUID
    owner_id: UUID
    name: str
    description: str | None
    visibility: Literal["private", "public"]
    created_at: datetime
    updated_at: datetime


class SpaceFileResponse(BaseModel):
    id: UUID
    space_id: UUID
    uploaded_by: UUID
    filename: str
    content_type: str | None
    size_bytes: int | None
    created_at: datetime


async def get_accessible_space(request: Request, space_id: UUID, user_id: UUID | None):
    async with database_session(request) as session:
        row = (await session.execute(text("""
            SELECT s.*
            FROM spaces AS s
            WHERE s.id = :space_id
              AND (s.visibility = 'public'
                   OR (:user_id IS NOT NULL AND (s.owner_id = :user_id OR EXISTS (
                       SELECT 1 FROM space_members AS m
                       WHERE m.space_id = s.id AND m.user_id = :user_id
                   ))))
        """), {"space_id": space_id, "user_id": user_id})).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, "Space не найден или доступ запрещён.")
    return row


async def require_space_write_access(request: Request, space_id: UUID, user_id: UUID):
    async with database_session(request) as session:
        allowed = (await session.execute(text("""
            SELECT EXISTS (
                SELECT 1 FROM spaces AS s
                WHERE s.id = :space_id AND s.owner_id = :user_id
            ) OR EXISTS (
                SELECT 1 FROM space_members AS m
                WHERE m.space_id = :space_id AND m.user_id = :user_id AND m.permission = 'write'
            )
        """), {"space_id": space_id, "user_id": user_id})).scalar_one()
    if not allowed:
        raise HTTPException(403, "Для загрузки нужен доступ write к Space.")


@router.post("", response_model=SpaceResponse, status_code=201)
async def create_space(payload: SpaceCreate, request: Request, user: CurrentUser):
    async with database_session(request) as session:
        row = (await session.execute(text("""
            INSERT INTO spaces (owner_id, name, description, visibility)
            VALUES (:owner_id, :name, :description, :visibility)
            RETURNING *
        """), {"owner_id": user.id, **payload.model_dump()})).mappings().one()
    return row


@router.get("", response_model=list[SpaceResponse])
async def list_spaces(request: Request, user: CurrentUser):
    async with database_session(request) as session:
        rows = (await session.execute(text("""
            SELECT DISTINCT s.*
            FROM spaces AS s
            LEFT JOIN space_members AS m ON m.space_id = s.id AND m.user_id = :user_id
            WHERE s.owner_id = :user_id OR m.user_id IS NOT NULL
            ORDER BY s.created_at DESC
        """), {"user_id": user.id})).mappings().all()
    return rows


@router.get("/{space_id}", response_model=SpaceResponse)
async def get_space(space_id: UUID, request: Request, user: OptionalCurrentUser):
    return await get_accessible_space(request, space_id, user.id if user else None)


@router.post("/{space_id}/members", status_code=204)
async def grant_space_access(
    space_id: UUID, payload: SpaceMemberCreate, request: Request, user: CurrentUser,
):
    async with database_session(request) as session:
        owner = (await session.execute(
            text("SELECT owner_id FROM spaces WHERE id = :space_id"), {"space_id": space_id}
        )).scalar_one_or_none()
        if owner is None:
            raise HTTPException(404, "Space не найден.")
        if owner != user.id:
            raise HTTPException(403, "Только автор Space может выдавать доступ.")
        if payload.user_id == owner:
            raise HTTPException(422, "Автор уже имеет полный доступ к Space.")
        await session.execute(text("""
            INSERT INTO space_members (space_id, user_id, permission, invited_by)
            VALUES (:space_id, :user_id, :permission, :invited_by)
            ON CONFLICT (space_id, user_id) DO UPDATE
                SET permission = EXCLUDED.permission, invited_by = EXCLUDED.invited_by
        """), {"space_id": space_id, **payload.model_dump(), "invited_by": user.id})


@router.post("/{space_id}/files", response_model=SpaceFileResponse, status_code=201)
async def upload_space_file(
    space_id: UUID, request: Request, user: CurrentUser, file: UploadFile,
):
    await require_space_write_access(request, space_id, user.id)
    storage = request.app.state.rustfs
    if storage is None:
        raise HTTPException(503, "File storage is unavailable.")
    filename = PurePath(file.filename or "file").name
    if not filename or filename in (".", "..") or "\x00" in filename:
        raise HTTPException(422, "Некорректное имя файла.")
    file_id = uuid4()
    object_key = f"spaces/{space_id}/{file_id}"
    try:
        await run_in_threadpool(
            storage.put_object,
            Bucket=os.getenv("RUSTFS_BUCKET", "deepsemantic"),
            Key=object_key,
            Body=file.file,
            **({"ContentType": file.content_type} if file.content_type else {}),
        )
    except Exception as error:
        raise HTTPException(503, "Не удалось сохранить файл.") from error
    bucket = os.getenv("RUSTFS_BUCKET", "deepsemantic")
    async with database_session(request) as session:
        row = (await session.execute(text("""
            INSERT INTO space_files
                (id, space_id, uploaded_by, bucket, object_key, filename, content_type, size_bytes)
            VALUES (:id, :space_id, :uploaded_by, :bucket, :object_key, :filename, :content_type, :size_bytes)
            RETURNING id, space_id, uploaded_by, filename, content_type, size_bytes, created_at
        """), {"id": file_id, "space_id": space_id, "uploaded_by": user.id, "bucket": bucket,
               "object_key": object_key, "filename": filename, "content_type": file.content_type,
               "size_bytes": file.size})).mappings().one()
    return row
