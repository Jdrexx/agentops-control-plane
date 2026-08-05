FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_CACHE_DIR=/tmp/uv-cache

RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.8.4
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY src ./src
COPY scripts ./scripts
RUN mkdir -p /data && chown -R 10001:10001 /app /data
USER 10001
EXPOSE 8110
CMD ["sh", "-c", ".venv/bin/uvicorn src.agentops.main:app --host 0.0.0.0 --port ${PORT:-8110}"]
