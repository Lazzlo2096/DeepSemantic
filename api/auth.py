import hmac
import os
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel


class AuthenticatedUser(BaseModel):
    id: UUID
    email: str
    role: Literal["user", "admin"]


async def authenticated_user(request: Request) -> AuthenticatedUser:
    expected = os.getenv("AUTH_PROXY_SECRET", "deepsemantic-local-proxy-secret")
    supplied = request.headers.get("x-auth-proxy-secret", "")
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "Требуется вход через доверенный прокси.")
    try:
        return AuthenticatedUser(
            id=request.headers["x-authenticated-user"],
            email=request.headers["x-authenticated-email"],
            role=request.headers["x-authenticated-role"],
        )
    except (KeyError, ValueError) as error:
        raise HTTPException(401, "Прокси не передал подтверждённого пользователя.") from error


CurrentUser = Annotated[AuthenticatedUser, Depends(authenticated_user)]


async def optional_authenticated_user(request: Request) -> AuthenticatedUser | None:
    if not request.headers.get("x-auth-proxy-secret"):
        return None
    return await authenticated_user(request)


OptionalCurrentUser = Annotated[AuthenticatedUser | None, Depends(optional_authenticated_user)]


async def administrator(user: CurrentUser) -> AuthenticatedUser:
    if user.role != "admin":
        raise HTTPException(403, "Требуются права администратора.")
    return user


AdminUser = Annotated[AuthenticatedUser, Depends(administrator)]
