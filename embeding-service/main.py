import asyncio
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from functools import partial
from threading import Lock

import aio_pika


MODEL_ID = "BAAI/bge-m3"
MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
DIMENSIONS = 1024
MAX_TEXTS = 32
logger = logging.getLogger(__name__)


def load_model(device: str):
    from FlagEmbedding import BGEM3FlagModel
    from huggingface_hub import snapshot_download

    model_path = snapshot_download(
        MODEL_ID,
        revision=MODEL_REVISION,
        allow_patterns=["*.json", "*.model", "*.bin", "*.pt"],
        ignore_patterns=["onnx/*"],
    )
    return BGEM3FlagModel(
        model_path,
        devices=device,
        use_fp16=device.startswith("cuda"),
        normalize_embeddings=True,
        return_dense=True,
        return_sparse=False,
        return_colbert_vecs=False,
    )


def encode(model, texts: list[str], max_tokens: int, batch_size: int):
    tokenized = model.tokenizer(
        texts,
        add_special_tokens=True,
        truncation=True,
        max_length=max_tokens + 1,
        return_length=True,
    )
    for index, length in enumerate(tokenized["length"]):
        if length > max_tokens:
            return {
                "status": 422,
                "error": {
                    "message": "Text exceeds the token limit",
                    "text_index": index,
                    "max_tokens": max_tokens,
                },
            }

    vectors = model.encode(texts, batch_size=batch_size, max_length=max_tokens)["dense_vecs"]
    return {
        "model": MODEL_ID,
        "revision": MODEL_REVISION,
        "dimensions": DIMENSIONS,
        "embeddings": vectors.astype("float32", copy=False).tolist(),
    }


@asynccontextmanager
async def embedding_backend(provider: str, openrouter_model: str):
    if provider == "openrouter":
        import httpx
        from openrouter_embeddings import embed

        key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise ValueError("OPENROUTER_API_KEY is required for OpenRouter embeddings")
        logger.info("Using OpenRouter embeddings with %s", openrouter_model)
        async with httpx.AsyncClient(
            base_url="https://openrouter.ai/api/v1/",
            headers={"Authorization": f"Bearer {key}"},
            timeout=float(os.getenv("EMBEDDING_RPC_TIMEOUT", "60")),
        ) as client:
            yield partial(embed, client, openrouter_model)
        return

    device = os.getenv("EMBEDDING_DEVICE", "cpu")
    max_tokens = int(os.getenv("EMBEDDING_MAX_TOKENS", "512"))
    batch_size = int(os.getenv("EMBEDDING_BATCH_SIZE", "8"))
    if not 3 <= max_tokens <= 8192:
        raise ValueError("EMBEDDING_MAX_TOKENS must be between 3 and 8192")
    if not 1 <= batch_size <= MAX_TEXTS:
        raise ValueError(f"EMBEDDING_BATCH_SIZE must be between 1 and {MAX_TEXTS}")

    logger.info("Loading %s on %s", MODEL_ID, device)
    model = await asyncio.to_thread(load_model, device)
    inference_lock = Lock()

    def infer(texts):
        with inference_lock:
            return encode(model, texts, max_tokens, batch_size)

    async def embed_local(texts):
        return await asyncio.to_thread(infer, texts)

    yield embed_local


async def main():
    provider = os.getenv("EMBEDDING_PROVIDER", "local")
    if provider not in ("local", "openrouter"):
        raise ValueError("EMBEDDING_PROVIDER must be local or openrouter")
    openrouter_model = os.getenv("EMBEDDING_OPENROUTER_MODEL", "baai/bge-m3").strip()
    queue_name = "embeddings.requests"
    if provider == "openrouter":
        if not openrouter_model:
            raise ValueError("EMBEDDING_OPENROUTER_MODEL is required for OpenRouter embeddings")
        model_key = hashlib.sha256(openrouter_model.encode()).hexdigest()[:16]
        queue_name = f"embeddings.requests.openrouter.{model_key}"
    async with (
        embedding_backend(provider, openrouter_model) as embed,
        await aio_pika.connect_robust(os.environ["AMQP_URL"], fail_fast=False) as connection,
    ):
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)
        queue = await channel.declare_queue(queue_name, durable=True)

        async def process(message):
            async with message.process(requeue=True):
                if not message.reply_to or not message.correlation_id:
                    return
                try:
                    response = await embed(json.loads(message.body)["texts"])
                except Exception:
                    logger.exception("Embedding inference failed")
                    response = {"status": 500, "error": "Embedding inference failed."}
                await channel.default_exchange.publish(
                    aio_pika.Message(
                        json.dumps(response).encode(),
                        content_type="application/json",
                        correlation_id=message.correlation_id,
                    ),
                    routing_key=message.reply_to,
                    mandatory=False,
                )

        await queue.consume(process)
        logger.info("Embedding provider %s is ready and consuming %s", provider, queue_name)
        await asyncio.Future()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
