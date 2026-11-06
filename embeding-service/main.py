import os
from contextlib import asynccontextmanager
from threading import Lock
from typing import Annotated

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, StringConstraints


MODEL_ID = "BAAI/bge-m3"
MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
DIMENSIONS = 1024
MAX_TEXTS = 32
MAX_CHARACTERS = 32_768

Text = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_CHARACTERS)
]


class EmbeddingRequest(BaseModel):
    texts: list[Text] = Field(min_length=1, max_length=MAX_TEXTS)


class EmbeddingResponse(BaseModel):
    model: str
    revision: str
    dimensions: int
    embeddings: list[list[float]]


@asynccontextmanager
async def lifespan(app: FastAPI):
    device = os.getenv("EMBEDDING_DEVICE", "cpu")
    max_tokens = int(os.getenv("EMBEDDING_MAX_TOKENS", "512"))
    batch_size = int(os.getenv("EMBEDDING_BATCH_SIZE", "8"))
    if not 3 <= max_tokens <= 8192:
        raise ValueError("EMBEDDING_MAX_TOKENS must be between 3 and 8192")
    if not 1 <= batch_size <= MAX_TEXTS:
        raise ValueError(f"EMBEDDING_BATCH_SIZE must be between 1 and {MAX_TEXTS}")

    from FlagEmbedding import BGEM3FlagModel
    from huggingface_hub import snapshot_download

    model_path = snapshot_download(
        MODEL_ID,
        revision=MODEL_REVISION,
        allow_patterns=["*.json", "*.model", "*.bin", "*.pt"],
        ignore_patterns=["onnx/*"],
    )
    app.state.model = BGEM3FlagModel(
        model_path,
        devices=device,
        use_fp16=device.startswith("cuda"),
        normalize_embeddings=True,
        return_dense=True,
        return_sparse=False,
        return_colbert_vecs=False,
    )
    app.state.max_tokens = max_tokens
    app.state.batch_size = batch_size
    app.state.inference_lock = Lock()
    try:
        yield
    finally:
        del app.state.model


app = FastAPI(title="BGE-M3 Embedding Service", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/embeddings", response_model=EmbeddingResponse)
def embeddings(payload: EmbeddingRequest, request: Request):
    state = request.app.state
    model = state.model
    with state.inference_lock:
        tokenized = model.tokenizer(
            payload.texts,
            add_special_tokens=True,
            truncation=True,
            max_length=state.max_tokens + 1,
            return_length=True,
        )
        for index, length in enumerate(tokenized["length"]):
            if length > state.max_tokens:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "message": "Text exceeds the token limit",
                        "text_index": index,
                        "max_tokens": state.max_tokens,
                    },
                )

        vectors = model.encode(
            payload.texts,
            batch_size=state.batch_size,
            max_length=state.max_tokens,
        )["dense_vecs"]

    return EmbeddingResponse(
        model=MODEL_ID,
        revision=MODEL_REVISION,
        dimensions=DIMENSIONS,
        embeddings=vectors.astype("float32", copy=False).tolist(),
    )
