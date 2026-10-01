"""Application factory.

All routes live under ``/api/v1`` (spec §39). Run locally with
``uv run poe api``; on Cloud Run the container starts uvicorn on ``$PORT``.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import chartlens_api
from chartlens_api.routers import health
from chartlens_core.config import ChartLensSettings, get_settings
from chartlens_core.logs import configure_logging

API_PREFIX = "/api/v1"

log = logging.getLogger("chartlens.api")


def create_app(settings: ChartLensSettings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.runtime)

    app = FastAPI(
        title="ChartLens API",
        version=chartlens_api.__version__,
        summary="See the structure. Read the trend.",
        docs_url=f"{API_PREFIX}/docs",
        openapi_url=f"{API_PREFIX}/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.api.cors_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.include_router(health.router, prefix=API_PREFIX)

    log.info(
        "ChartLens API %s starting (env=%s, methodology=%s)",
        chartlens_api.__version__,
        settings.runtime.environment,
        settings.methodology_hash(),
    )
    return app


app = create_app()
