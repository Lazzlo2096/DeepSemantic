FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 app

COPY main.py database.py chunks.py papers.py embedding_schemas.py paper_embeddings.py paper_summaries.py .

FROM base AS summarizer
RUN python -m pip install --no-cache-dir 'boto3>=1.36,<2' 'tokenizers>=0.22,<1'
ADD --checksum=sha256:4667f2089529e8e7657cfb6d1c19910ae71ff5f28aa7ab2ff2763330affad795 --chmod=644 https://huggingface.co/unsloth/gemma-3-4b-it/resolve/bd8f0d2/tokenizer.json /app/gemma-tokenizer.json
COPY paper_summarizer.py .
USER app
CMD ["python", "-m", "paper_summarizer"]

FROM base AS api
USER app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
