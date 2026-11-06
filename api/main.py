import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

import aio_pika
from aio_pika.exceptions import AMQPException
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, StringConstraints


QUEUE = "embeddings.requests"
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32_768)]


class EmbeddingRequest(BaseModel):
    texts: list[Text] = Field(min_length=1, max_length=32)


class EmbeddingResponse(BaseModel):
    model: str
    revision: str
    dimensions: int
    embeddings: list[list[float]]


class EmbeddingRPC:
    def __init__(self, channel, replies, timeout: float):
        self.channel = channel
        self.replies = replies
        self.timeout = timeout
        self.pending: dict[str, asyncio.Future] = {}

    async def on_reply(self, message):
        future = self.pending.get(message.correlation_id)
        if future is not None and not future.done():
            future.set_result(message.body)

    async def check_worker(self):
        try:
            async with asyncio.timeout(3):
                # aio-pika caches passive declarations on robust channels.
                queue = await self.channel.declare_queue(QUEUE, durable=True, robust=False)
        except (AMQPException, OSError, TimeoutError) as error:
            raise HTTPException(503, "RabbitMQ is unavailable.") from error
        if not queue.declaration_result.consumer_count:
            raise HTTPException(503, "No embedding worker is ready.")

    async def embed(self, payload: EmbeddingRequest):
        await self.check_worker()
        correlation_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[correlation_id] = future
        try:
            async with asyncio.timeout(self.timeout):
                await self.channel.default_exchange.publish(
                    aio_pika.Message(
                        payload.model_dump_json().encode(),
                        content_type="application/json",
                        correlation_id=correlation_id,
                        reply_to=self.replies.name,
                        expiration=self.timeout,
                    ),
                    routing_key=QUEUE,
                )
                result = json.loads(await future)
        except TimeoutError as error:
            raise HTTPException(504, "Embedding worker response timed out.") from error
        except (AMQPException, OSError) as error:
            raise HTTPException(503, "RabbitMQ is unavailable.") from error
        finally:
            self.pending.pop(correlation_id, None)
            future.cancel()

        if "error" in result:
            raise HTTPException(result["status"], result["error"])
        return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    timeout = float(os.getenv("EMBEDDING_RPC_TIMEOUT", "60"))
    if not 0 < timeout < float("inf"):
        raise ValueError("EMBEDDING_RPC_TIMEOUT must be finite and positive")
    async with await aio_pika.connect_robust(os.environ["AMQP_URL"], timeout=5) as connection:
        channel = await connection.channel(on_return_raises=True)
        await channel.declare_queue(QUEUE, durable=True)
        replies = await channel.declare_queue(f"embeddings.reply.{uuid4().hex}", exclusive=True)
        rpc = EmbeddingRPC(channel, replies, timeout)
        await replies.consume(rpc.on_reply, no_ack=True)
        app.state.rpc = rpc
        yield


app = FastAPI(title="BGE-M3 Embedding Service", lifespan=lifespan)


@app.get("/health")
async def health(request: Request):
    await request.app.state.rpc.check_worker()
    return {"status": "ok"}


@app.post("/embeddings", response_model=EmbeddingResponse)
async def embeddings(payload: EmbeddingRequest, request: Request):
    return await request.app.state.rpc.embed(payload)
