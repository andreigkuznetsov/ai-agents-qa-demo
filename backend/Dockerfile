FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

FROM python:3.13.18-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72 AS base
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

FROM base AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-cache

FROM dependencies AS checks
RUN uv sync --locked --no-cache
COPY app/ app/
COPY migrations/ migrations/
COPY tests/ tests/
RUN .venv/bin/ruff check app migrations tests \
    && .venv/bin/ruff format --check app migrations tests \
    && .venv/bin/mypy app migrations tests \
    && .venv/bin/python -m pytest

FROM base AS runtime
COPY --from=dependencies /app/.venv .venv/
COPY app/ app/
COPY migrations/ migrations/
ENV UVICORN_HOST=0.0.0.0 UVICORN_PORT=8000 WEB_CONCURRENCY=1
USER 10001:10001
EXPOSE 8000
CMD ["/app/.venv/bin/python", "-m", "uvicorn", "app.main:app", "--no-access-log"]
