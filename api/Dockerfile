FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 app

COPY main.py database.py chunks.py papers.py embedding_schemas.py paper_chunking.py paper_summaries.py rag.py rag_history.py rag_model.py llm_chat.py embedding_service.py embeddings_config.py storage.py paper_repository.py temporal_runtime.py .

COPY processing/__init__.py processing/config.py ./processing/
COPY pipeline.py .
COPY paper_index_status.py .

FROM base AS worker-control
COPY worker_control.py .
CMD ["python", "-m", "uvicorn", "worker_control:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS api
USER app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
