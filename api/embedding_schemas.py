import hashlib
import os
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints


EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "local")
if EMBEDDING_PROVIDER not in ("local", "openrouter"):
    raise ValueError("EMBEDDING_PROVIDER must be local or openrouter")
EMBEDDING_OPENROUTER_MODEL = os.getenv("EMBEDDING_OPENROUTER_MODEL", "baai/bge-m3").strip()
if EMBEDDING_PROVIDER == "openrouter" and not EMBEDDING_OPENROUTER_MODEL:
    raise ValueError("EMBEDDING_OPENROUTER_MODEL is required for OpenRouter embeddings")
EMBEDDING_MODEL_KEY = hashlib.sha256(EMBEDDING_OPENROUTER_MODEL.encode()).hexdigest()[:16]
EMBEDDING_QUEUE = (
    f"embeddings.requests.openrouter.{EMBEDDING_MODEL_KEY}"
    if EMBEDDING_PROVIDER == "openrouter" else "embeddings.requests"
)


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32_768)]


class EmbeddingRequest(BaseModel):
    texts: list[Text] = Field(min_length=1, max_length=32)


class EmbeddingResponse(BaseModel):
    model: str
    revision: str
    dimensions: int
    embeddings: list[list[float]]
