# syntax=docker/dockerfile:1.7
#
# One Dockerfile, two deployable targets:
#   docker build -f docker/Dockerfile --target api .       → Cloud Run service
#   docker build -f docker/Dockerfile --target pipeline .  → batch jobs (GitHub Actions / Cloud Run Jobs)
#
# Build context is the repository root. Dependencies are resolved from uv.lock
# (--frozen), so images are reproducible from a commit.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# ------------------------------------------------------------------ builder base
FROM ${PYTHON_IMAGE} AS builder
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src

# Manifests first so the dependency layer is cached independently of source changes.
COPY pyproject.toml uv.lock ./
COPY core/pyproject.toml core/
COPY engine/pyproject.toml engine/
COPY pipeline/pyproject.toml pipeline/
COPY backend/pyproject.toml backend/

# ------------------------------------------------------------------ api build
FROM builder AS build-api
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package chartlens-api
COPY core core
COPY engine engine
COPY pipeline pipeline
COPY backend backend
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package chartlens-api

# ------------------------------------------------------------------ pipeline build
FROM builder AS build-pipeline
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package chartlens-pipeline
COPY core core
COPY pipeline pipeline
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package chartlens-pipeline

# ------------------------------------------------------------------ runtime base
FROM ${PYTHON_IMAGE} AS runtime
RUN useradd --system --uid 10001 --home-dir /app chartlens
WORKDIR /app
COPY config /app/config
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    CHARTLENS_CONFIG_FILE=/app/config/chartlens.toml \
    CHARTLENS_RUNTIME__LOG_JSON=true

# ------------------------------------------------------------------ targets
FROM runtime AS api
COPY --from=build-api /app/.venv /app/.venv
USER 10001
ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn chartlens_api.main:app --host 0.0.0.0 --port ${PORT}"]

FROM runtime AS pipeline
COPY --from=build-pipeline /app/.venv /app/.venv
USER 10001
ENTRYPOINT ["chartlens-pipeline"]
CMD ["info"]
