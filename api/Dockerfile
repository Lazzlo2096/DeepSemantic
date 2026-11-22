FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 app

COPY *.py ./
COPY agents/ ./agents/
COPY processing/ ./processing/

FROM base AS worker-control
COPY worker_control.py .
CMD ["python", "-m", "uvicorn", "worker_control:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS api
USER app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
