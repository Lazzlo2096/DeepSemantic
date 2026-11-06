import asyncio
import json
import logging
import os
from threading import Lock

import aio_pika


QUEUE = "embeddings.requests"
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


async def main():
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

    def infer(body):
        texts = json.loads(body)["texts"]
        with inference_lock:
            return encode(model, texts, max_tokens, batch_size)

    async with await aio_pika.connect_robust(os.environ["AMQP_URL"], fail_fast=False) as connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)
        queue = await channel.declare_queue(QUEUE, durable=True)

        async def process(message):
            async with message.process(requeue=True):
                if not message.reply_to or not message.correlation_id:
                    return
                try:
                    response = await asyncio.to_thread(infer, message.body)
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
        logger.info("%s is ready and consuming %s", MODEL_ID, QUEUE)
        await asyncio.Future()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
