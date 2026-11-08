FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY summary-worker/requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 app

ADD --checksum=sha256:4667f2089529e8e7657cfb6d1c19910ae71ff5f28aa7ab2ff2763330affad795 --chmod=644 https://huggingface.co/unsloth/gemma-3-4b-it/resolve/bd8f0d2/tokenizer.json /app/gemma-tokenizer.json
COPY summary-worker/main.py api/summary_schema.py .
USER app

CMD ["python", "main.py"]
