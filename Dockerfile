# syntax=docker/dockerfile:1
FROM python:3.11-slim

WORKDIR /app

# OpenMP runtime used by torch/numpy on CPU
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

# Layer cache: deps first, then app code
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && grep -vE '^torch([><=! ]|$)' requirements.txt > /tmp/requirements.no-torch.txt \
    && pip install --no-cache-dir -r /tmp/requirements.no-torch.txt \
    && rm /tmp/requirements.no-torch.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/app/.cache/huggingface \
    TRANSFORMERS_CACHE=/app/.cache/huggingface

EXPOSE 8000

# Bind 0.0.0.0 so the service is reachable outside the container.
# Host port is mapped via APP_PORT in docker-compose (default 3020).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
