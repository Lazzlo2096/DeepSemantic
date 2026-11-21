import os
from dataclasses import dataclass

from sqlalchemy.engine import URL


def enabled(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: URL
    cookie_name: str
    csrf_cookie_name: str
    cookie_secure: bool
    session_days: int
    invite_hours: int
    public_url: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=URL.create(
                "postgresql+psycopg",
                host=os.getenv("POSTGRES_HOST", "postgres"),
                database=os.environ["POSTGRES_DB"],
                username=os.environ["POSTGRES_USER"],
                password=os.environ["POSTGRES_PASSWORD"],
            ),
            cookie_name=os.getenv("AUTH_COOKIE_NAME", "deepsemantic_session"),
            csrf_cookie_name=os.getenv("AUTH_CSRF_COOKIE_NAME", "deepsemantic_csrf"),
            cookie_secure=enabled("AUTH_COOKIE_SECURE"),
            session_days=int(os.getenv("AUTH_SESSION_DAYS", "30")),
            invite_hours=int(os.getenv("AUTH_INVITE_HOURS", "72")),
            public_url=os.getenv("AUTH_PUBLIC_URL", "http://127.0.0.1:8080").rstrip("/"),
        )
