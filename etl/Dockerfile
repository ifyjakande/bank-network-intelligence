# syntax=docker/dockerfile:1.7
# build context is the repo root: the image carries the migrations it applies
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY etl/pyproject.toml etl/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY etl/src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
RUN useradd --system --uid 10001 --no-create-home flowetl
COPY --from=build --chown=flowetl /app/.venv /app/.venv
COPY clickhouse/migrations /app/migrations
COPY etl/seeds /app/seeds
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
USER flowetl
EXPOSE 9103
ENTRYPOINT ["flowetl"]
