"""Shared embeddings service hiding provider selection and HTTP transport."""
import logging
import math
from contextlib import asynccontextmanager

import httpx
from fastapi import HTTPException

from embeddings_config import EmbeddingSettings
from embedding_schemas import EMBEDDING_OPENROUTER_MODEL, EmbeddingRequest


logger = logging.getLogger(__name__)


async def embed(client: httpx.AsyncClient, model: str, texts: list[str]) -> dict:
    try:
        response = await client.post("embeddings", json={
            "model": model,
            "input": texts,
            "encoding_format": "float",
        })
        response.raise_for_status()
    except httpx.TimeoutException:
        return {"status": 504, "error": "Embedding request timed out."}
    except httpx.RequestError as error:
        logger.warning("Embeddings connection failed: %s", type(error).__name__)
        return {"status": 503, "error": "Embeddings are unavailable."}
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        logger.warning("Embeddings returned HTTP %d", status)
        if status in (400, 413, 422):
            return {"status": 422, "error": "Embedding service rejected the embedding input."}
        return {"status": 503, "error": "Embeddings are unavailable."}

    try:
        items = sorted(response.json()["data"], key=lambda item: item["index"])
        if [item["index"] for item in items] != list(range(len(texts))):
            raise ValueError("Unexpected embedding indices")
        dimensions = len(items[0]["embedding"])
        if not dimensions or (model == "baai/bge-m3" and dimensions != 1024):
            raise ValueError("Unexpected embedding dimension")
        embeddings = [normalize(item["embedding"], dimensions) for item in items]
    except (IndexError, KeyError, TypeError, ValueError, OverflowError) as error:
        logger.warning("Invalid embeddings response: %s", type(error).__name__)
        return {"status": 502, "error": "Embedding service returned invalid embeddings."}

    # Preserve the existing storage identity when switching the compatible endpoint.
    return {"model": model, "revision": "openrouter", "dimensions": dimensions, "embeddings": embeddings}


def normalize(vector: list[float], dimensions: int) -> list[float]:
    if not isinstance(vector, list) or len(vector) != dimensions:
        raise ValueError("Unexpected embedding dimension")
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or not norm:
        raise ValueError("Invalid embedding norm")
    return [value / norm for value in vector]


class Embeddings:
    def __init__(self, client: httpx.AsyncClient, settings: EmbeddingSettings):
        self.client = client
        self.settings = settings

    def check_configured(self):
        if self.settings.provider == "openrouter" and not self.settings.api_key:
            raise HTTPException(503, "OPENROUTER_API_KEY is required for OpenRouter embeddings.")

    async def embed(self, payload: EmbeddingRequest):
        self.check_configured()
        result = await embed(self.client, EMBEDDING_OPENROUTER_MODEL, payload.texts)
        if "error" in result:
            raise HTTPException(result["status"], result["error"])
        return result


@asynccontextmanager
async def create_embeddings(*, transport: httpx.AsyncBaseTransport | None = None):
    settings = EmbeddingSettings.from_env()
    headers = {"Authorization": f"Bearer {settings.api_key}"} if settings.api_key else {}
    async with httpx.AsyncClient(
        base_url=settings.base_url, timeout=settings.timeout, headers=headers, transport=transport,
    ) as client:
        yield Embeddings(client, settings)
