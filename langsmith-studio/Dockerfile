FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/api

WORKDIR /app
COPY api/requirements.txt ./api/requirements.txt
RUN python -m pip install --no-cache-dir -r api/requirements.txt \
    && useradd --create-home --uid 10001 app

COPY api/main.py api/database.py api/chunks.py api/papers.py api/embedding_schemas.py api/paper_chunking.py api/paper_summaries.py api/rag.py api/rag_history.py api/rag_model.py api/llm_chat.py api/openrouter_embeddings.py api/storage.py ./api/

COPY langsmith-studio/requirements.txt ./langsmith-studio/requirements.txt
RUN python -m pip install --no-cache-dir -r langsmith-studio/requirements.txt \
    && mkdir -p /app/langsmith-studio/.langgraph_api \
    && chown app:app /app/langsmith-studio/.langgraph_api
COPY langsmith-studio/studio.py langsmith-studio/studio_http.py langsmith-studio/langgraph.json ./langsmith-studio/
WORKDIR /app/langsmith-studio
USER app
EXPOSE 2024
CMD ["langgraph", "dev", "--host", "0.0.0.0", "--port", "2024", "--no-browser", "--no-reload", "--allow-blocking"]
