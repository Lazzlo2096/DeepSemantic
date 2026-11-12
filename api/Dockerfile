FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 app

COPY main.py database.py chunks.py papers.py embedding_schemas.py paper_chunking.py paper_embeddings.py paper_summaries.py summary_schema.py rag.py rag_history.py llm_chat.py openrouter_embeddings.py storage.py .

FROM base AS worker-control
COPY worker_control.py .
CMD ["python", "-m", "uvicorn", "worker_control:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS embeding-worker
RUN python -m pip install --no-cache-dir 'tokenizers==0.22.2' 'beautifulsoup4>=4.12,<5'
# Tokenization for article chunking; inference runs through OpenRouter.
ADD --checksum=sha256:21106b6d7dab2952c1d496fb21d5dc9db75c28ed361a05f5020bbba27810dd08 --chmod=644 https://huggingface.co/BAAI/bge-m3/resolve/5617a9f61b028005a4858fdac845db406aefb181/tokenizer.json /app/bge-m3-tokenizer.json
USER app

FROM base AS api
USER app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
