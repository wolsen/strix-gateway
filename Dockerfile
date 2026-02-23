# FILE: Dockerfile
FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --system

COPY apollo_gateway/ ./apollo_gateway/

CMD ["uvicorn", "apollo_gateway.main:app", "--host", "0.0.0.0", "--port", "8080"]
