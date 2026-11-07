FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/var/cache/huggingface

WORKDIR /app

ARG TORCH_VARIANT=cpu
RUN python -m pip install --no-cache-dir \
    --extra-index-url "https://download.pytorch.org/whl/${TORCH_VARIANT}" \
    "torch==2.7.1+${TORCH_VARIANT}"

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 10001 app \
    && install -d -o app -g app "$HF_HOME"

COPY main.py .
USER app

CMD ["python", "main.py"]
