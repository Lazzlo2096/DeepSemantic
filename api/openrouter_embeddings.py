import logging
import math

import httpx
from fastapi import HTTPException

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
        return {"status": 504, "error": "DeepKaravan embedding request timed out."}
    except httpx.RequestError as error:
        logger.warning("DeepKaravan embeddings connection failed: %s", type(error).__name__)
        return {"status": 503, "error": "DeepKaravan embeddings are unavailable."}
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        logger.warning("DeepKaravan embeddings returned HTTP %d", status)
        if status in (400, 413, 422):
            return {"status": 422, "error": "DeepKaravan rejected the embedding input."}
        return {"status": 503, "error": "DeepKaravan embeddings are unavailable."}

    try:
        items = sorted(response.json()["data"], key=lambda item: item["index"])
        if [item["index"] for item in items] != list(range(len(texts))):
            raise ValueError("Unexpected embedding indices")
        dimensions = len(items[0]["embedding"])
        if not dimensions or (model == "baai/bge-m3" and dimensions != 1024):
            raise ValueError("Unexpected embedding dimension")
        embeddings = [normalize(item["embedding"], dimensions) for item in items]
    except (IndexError, KeyError, TypeError, ValueError, OverflowError) as error:
        logger.warning("Invalid DeepKaravan embeddings response: %s", type(error).__name__)
        return {"status": 502, "error": "DeepKaravan returned invalid embeddings."}

    # Preserve the existing storage identity when switching the compatible endpoint.
    return {"model": model, "revision": "openrouter", "dimensions": dimensions, "embeddings": embeddings}


def normalize(vector: list[float], dimensions: int) -> list[float]:
    if not isinstance(vector, list) or len(vector) != dimensions:
        raise ValueError("Unexpected embedding dimension")
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or not norm:
        raise ValueError("Invalid embedding norm")
    return [value / norm for value in vector]


class OpenRouterEmbeddings:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def embed(self, payload: EmbeddingRequest):
        result = await embed(self.client, EMBEDDING_OPENROUTER_MODEL, payload.texts)
        if "error" in result:
            raise HTTPException(result["status"], result["error"])
        return result
