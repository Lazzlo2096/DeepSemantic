import asyncio
import hmac
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .config import Settings
from .security import DUMMY_PASSWORD_HASH, hash_password, new_token, normalize_email, token_hash, verify_password


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class RegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=12, max_length=1024)


class InviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=320)
    role: Literal["user", "admin"] = "user"
    expires_in_hours: int | None = Field(default=None, ge=1, le=24 * 30)


class InviteResponse(BaseModel):
    email: str
    role: Literal["user", "admin"]
    expires_at: datetime
    invite_url: str


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "admin"] | None = None
    disabled: bool | None = None


class User(BaseModel):
    id: UUID
    email: str
    role: Literal["user", "admin"]
    created_at: datetime
    disabled_at: datetime | None = None


class AuthenticatedUser(User):
    token_digest: bytes = Field(exclude=True)
    csrf_token_hash: bytes | None = Field(exclude=True)
    cookie_authenticated: bool = Field(exclude=True)


class LoginLimiter:
    def __init__(self, attempts: int = 8, window_seconds: int = 15 * 60):
        self.attempts = attempts
        self.window_seconds = window_seconds
        self.failures: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self.lock = asyncio.Lock()

    async def check(self, key: tuple[str, str]) -> None:
        async with self.lock:
            attempts = self.failures[key]
            cutoff = time.monotonic() - self.window_seconds
            while attempts and attempts[0] < cutoff:
                attempts.popleft()
            if len(attempts) >= self.attempts:
                raise HTTPException(429, "Слишком много попыток входа. Попробуйте позже.")

    async def failed(self, key: tuple[str, str]) -> None:
        async with self.lock:
            self.failures[key].append(time.monotonic())

    async def succeeded(self, key: tuple[str, str]) -> None:
        async with self.lock:
            self.failures.pop(key, None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    engine = create_async_engine(
        settings.database_url,
        connect_args={"connect_timeout": 5},
        pool_size=4,
        max_overflow=0,
        pool_pre_ping=True,
    )
    app.state.settings = settings
    app.state.db_sessions = async_sessionmaker(engine, expire_on_commit=False)
    app.state.login_limiter = LoginLimiter()
    try:
        yield
    finally:
        await engine.dispose()


app = FastAPI(title="DeepSemantic Auth", lifespan=lifespan)


@app.middleware("http")
async def prevent_auth_response_caching(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/auth/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def ensure_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    parsed = urlsplit(origin)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    if parsed.netloc.lower() != host.lower() or parsed.scheme.lower() != scheme.lower():
        raise HTTPException(403, "Запрос с другого сайта отклонён.")


def authentication_error(request: Request, detail: str) -> HTTPException:
    forwarded_uri = request.headers.get("x-forwarded-uri", "")
    accepts_html = "text/html" in request.headers.get("accept", "")
    if accepts_html and forwarded_uri.startswith("/") and not forwarded_uri.startswith("//"):
        from urllib.parse import urlencode
        return HTTPException(303, detail, headers={"Location": f"/login.html?{urlencode({'next': forwarded_uri})}"})
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail)


def presented_token(request: Request) -> tuple[str, bool]:
    authorization = request.headers.get("authorization", "")
    if authorization:
        scheme, separator, token = authorization.partition(" ")
        if separator and scheme.lower() == "bearer" and token:
            return token, False
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Некорректный заголовок Authorization.")
    token = request.cookies.get(request.app.state.settings.cookie_name)
    if not token:
        raise authentication_error(request, "Требуется вход.")
    return token, True


async def current_user(request: Request) -> AuthenticatedUser:
    token, from_cookie = presented_token(request)
    try:
        digest = token_hash(token)
    except UnicodeEncodeError as error:
        raise authentication_error(request, "Сессия недействительна.") from error
    async with request.app.state.db_sessions() as session:
        row = (await session.execute(text("""
            SELECT users.id, users.email, users.role, users.created_at, users.disabled_at,
                   sessions.csrf_token_hash
            FROM auth.sessions AS sessions
            JOIN auth.users AS users ON users.id = sessions.user_id
            WHERE sessions.token_hash = :token_hash
              AND sessions.revoked_at IS NULL
              AND sessions.expires_at > now()
              AND users.disabled_at IS NULL
        """), {"token_hash": digest})).mappings().one_or_none()
    if row is None:
        raise authentication_error(request, "Сессия недействительна или истекла.")
    return AuthenticatedUser(
        **row,
        token_digest=digest,
        cookie_authenticated=from_cookie,
    )


CurrentUser = Annotated[AuthenticatedUser, Depends(current_user)]


async def admin_user(user: CurrentUser) -> AuthenticatedUser:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Требуются права администратора.")
    return user


AdminUser = Annotated[AuthenticatedUser, Depends(admin_user)]


def enforce_csrf(request: Request, user: AuthenticatedUser, method: str | None = None) -> None:
    if not user.cookie_authenticated or (method or request.method).upper() in {"GET", "HEAD", "OPTIONS"}:
        return
    supplied = request.headers.get("x-csrf-token", "")
    if not supplied or user.csrf_token_hash is None:
        raise HTTPException(403, "CSRF-токен отсутствует.")
    try:
        digest = token_hash(supplied)
    except UnicodeEncodeError as error:
        raise HTTPException(403, "CSRF-токен недействителен.") from error
    if not hmac.compare_digest(digest, user.csrf_token_hash):
        raise HTTPException(403, "CSRF-токен недействителен.")
    ensure_same_origin(request)


def public_user(user: AuthenticatedUser) -> User:
    return User(**user.model_dump())


async def create_session(request: Request, response: Response, user_id: UUID) -> None:
    settings: Settings = request.app.state.settings
    session_token = new_token()
    csrf_token = new_token()
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.session_days)
    async with request.app.state.db_sessions.begin() as session:
        await session.execute(text("""
            INSERT INTO auth.sessions (token_hash, csrf_token_hash, user_id, expires_at)
            VALUES (:token_hash, :csrf_token_hash, :user_id, :expires_at)
        """), {
            "token_hash": token_hash(session_token),
            "csrf_token_hash": token_hash(csrf_token),
            "user_id": user_id,
            "expires_at": expires_at,
        })
    max_age = settings.session_days * 24 * 60 * 60
    response.set_cookie(
        settings.cookie_name,
        session_token,
        max_age=max_age,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf_token,
        max_age=max_age,
        secure=settings.cookie_secure,
        httponly=False,
        samesite="strict",
        path="/",
    )


def clear_session_cookies(request: Request, response: Response) -> None:
    settings: Settings = request.app.state.settings
    for name in (settings.cookie_name, settings.csrf_cookie_name):
        response.delete_cookie(name, secure=settings.cookie_secure, httponly=name == settings.cookie_name,
                               samesite="strict", path="/")


@app.get("/health/live", include_in_schema=False)
async def live():
    return {"status": "ok"}


@app.post("/auth/login", response_model=User)
async def login(payload: LoginRequest, request: Request, response: Response):
    ensure_same_origin(request)
    try:
        email = normalize_email(payload.email)
    except ValueError as error:
        raise HTTPException(422, "Укажите корректный адрес электронной почты.") from error
    forwarded = request.headers.get("x-forwarded-for", "").partition(",")[0].strip()
    address = forwarded or (request.client.host if request.client else "unknown")
    key = (address, email)
    limiter: LoginLimiter = request.app.state.login_limiter
    await limiter.check(key)
    async with request.app.state.db_sessions() as session:
        row = (await session.execute(text("""
            SELECT id, email, password_hash, role, created_at, disabled_at
            FROM auth.users WHERE lower(email) = :email
        """), {"email": email})).mappings().one_or_none()
    candidate_hash = row["password_hash"] if row is not None else DUMMY_PASSWORD_HASH
    valid = await asyncio.to_thread(verify_password, candidate_hash, payload.password)
    valid = row is not None and valid
    if not valid or row["disabled_at"] is not None:
        await limiter.failed(key)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный адрес или пароль.")
    await limiter.succeeded(key)
    await create_session(request, response, row["id"])
    return User(**row)


@app.post("/auth/register", response_model=User, status_code=201)
async def register(payload: RegistrationRequest, request: Request, response: Response):
    ensure_same_origin(request)
    digest = token_hash(payload.token)
    password_hash = await asyncio.to_thread(hash_password, payload.password)
    async with request.app.state.db_sessions.begin() as session:
        invite = (await session.execute(text("""
            SELECT email, role FROM auth.invites
            WHERE token_hash = :token_hash
              AND consumed_at IS NULL
              AND revoked_at IS NULL
              AND expires_at > now()
            FOR UPDATE
        """), {"token_hash": digest})).mappings().one_or_none()
        if invite is None:
            raise HTTPException(400, "Приглашение недействительно или истекло.")
        user = (await session.execute(text("""
            INSERT INTO auth.users (email, password_hash, role)
            VALUES (:email, :password_hash, :role)
            ON CONFLICT (lower(email)) DO NOTHING
            RETURNING id, email, role, created_at, disabled_at
        """), {**invite, "password_hash": password_hash})).mappings().one_or_none()
        if user is None:
            raise HTTPException(409, "Аккаунт с таким адресом уже существует.")
        await session.execute(text("""
            UPDATE auth.invites SET consumed_at = now() WHERE token_hash = :token_hash
        """), {"token_hash": digest})
    await create_session(request, response, user["id"])
    return User(**user)


@app.get("/auth/me", response_model=User)
async def me(user: CurrentUser):
    return public_user(user)


@app.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response, user: CurrentUser):
    enforce_csrf(request, user)
    async with request.app.state.db_sessions.begin() as session:
        await session.execute(text("""
            UPDATE auth.sessions SET revoked_at = now()
            WHERE token_hash = :token_hash AND revoked_at IS NULL
        """), {"token_hash": user.token_digest})
    clear_session_cookies(request, response)


@app.get("/auth/users", response_model=list[User])
async def list_users(request: Request, _: AdminUser):
    async with request.app.state.db_sessions() as session:
        rows = (await session.execute(text("""
            SELECT id, email, role, created_at, disabled_at
            FROM auth.users ORDER BY created_at, id
        """))).mappings().all()
    return [User(**row) for row in rows]


@app.patch("/auth/users/{user_id}", response_model=User)
async def update_user(user_id: UUID, payload: UserUpdate, request: Request, administrator: AdminUser):
    enforce_csrf(request, administrator)
    if user_id == administrator.id and (payload.disabled is True or payload.role == "user"):
        raise HTTPException(409, "Нельзя отключить свой аккаунт или снять с него роль администратора.")
    values = payload.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(422, "Не указаны изменения.")
    assignments = []
    params: dict = {"id": user_id}
    if "role" in values:
        assignments.append("role = :role")
        params["role"] = values["role"]
    if "disabled" in values:
        assignments.append("disabled_at = CASE WHEN :disabled THEN now() ELSE NULL END")
        params["disabled"] = values["disabled"]
    async with request.app.state.db_sessions.begin() as session:
        active_admins = (await session.execute(text("""
            SELECT id FROM auth.users
            WHERE role = 'admin' AND disabled_at IS NULL
            FOR UPDATE
        """))).scalars().all()
        removes_admin = payload.role == "user" or payload.disabled is True
        if user_id in active_admins and removes_admin and len(active_admins) == 1:
            raise HTTPException(409, "В системе должен остаться хотя бы один активный администратор.")
        row = (await session.execute(text(f"""
            UPDATE auth.users SET {', '.join(assignments)} WHERE id = :id
            RETURNING id, email, role, created_at, disabled_at
        """), params)).mappings().one_or_none()
        if row is None:
            raise HTTPException(404, "Пользователь не найден.")
        if values.get("disabled") is True:
            await session.execute(text("""
                UPDATE auth.sessions SET revoked_at = now()
                WHERE user_id = :id AND revoked_at IS NULL
            """), {"id": user_id})
    return User(**row)


@app.post("/auth/invites", response_model=InviteResponse, status_code=201)
async def create_invite(payload: InviteRequest, request: Request, administrator: AdminUser):
    enforce_csrf(request, administrator)
    try:
        email = normalize_email(payload.email)
    except ValueError as error:
        raise HTTPException(422, "Укажите корректный адрес электронной почты.") from error
    raw_token = new_token()
    expires = datetime.now(timezone.utc) + timedelta(
        hours=payload.expires_in_hours or request.app.state.settings.invite_hours
    )
    async with request.app.state.db_sessions.begin() as session:
        existing = await session.scalar(text("SELECT id FROM auth.users WHERE lower(email) = :email"), {"email": email})
        if existing is not None:
            raise HTTPException(409, "Аккаунт с таким адресом уже существует.")
        await session.execute(text("""
            UPDATE auth.invites SET revoked_at = now()
            WHERE lower(email) = :email AND consumed_at IS NULL AND revoked_at IS NULL
        """), {"email": email})
        await session.execute(text("""
            INSERT INTO auth.invites (token_hash, email, role, invited_by, expires_at)
            VALUES (:token_hash, :email, :role, :invited_by, :expires_at)
        """), {
            "token_hash": token_hash(raw_token),
            "email": email,
            "role": payload.role,
            "invited_by": administrator.id,
            "expires_at": expires,
        })
    return {
        "email": email,
        "role": payload.role,
        "expires_at": expires,
        "invite_url": f"{request.app.state.settings.public_url}/login.html#invite={raw_token}",
    }


async def verification_response(request: Request, response: Response, user: AuthenticatedUser, role: str | None) -> None:
    method = request.headers.get("x-forwarded-method", "GET")
    enforce_csrf(request, user, method)
    if role and user.role != role:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Недостаточно прав.")
    response.status_code = 204
    response.headers["X-Authenticated-User"] = str(user.id)
    response.headers["X-Authenticated-Role"] = user.role
    response.headers["X-Authenticated-Email"] = user.email


@app.get("/verify", status_code=204)
async def verify(request: Request, response: Response, user: CurrentUser):
    await verification_response(request, response, user, None)


@app.get("/verify/admin", status_code=204)
async def verify_admin(request: Request, response: Response, user: CurrentUser):
    await verification_response(request, response, user, "admin")
