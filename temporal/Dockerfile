FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/api:/app
WORKDIR /app
COPY api/requirements.txt /tmp/api-requirements.txt
COPY arxiv-grabber/requirements.txt /tmp/arxiv-requirements.txt
COPY temporal/requirements.txt /tmp/temporal-requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/api-requirements.txt -r /tmp/arxiv-requirements.txt -r /tmp/temporal-requirements.txt \
    && useradd --create-home --uid 10001 app
COPY api /app/api
COPY arxiv-grabber /app/arxiv_grabber
ADD --checksum=sha256:21106b6d7dab2952c1d496fb21d5dc9db75c28ed361a05f5020bbba27810dd08 --chmod=644 https://huggingface.co/BAAI/bge-m3/resolve/5617a9f61b028005a4858fdac845db406aefb181/tokenizer.json /app/bge-m3-tokenizer.json
USER app
CMD ["python", "-m", "processing.dispatcher"]
