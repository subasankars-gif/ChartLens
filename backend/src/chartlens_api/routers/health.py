from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

import chartlens_api
import chartlens_core
import chartlens_engine
import chartlens_pipeline
from chartlens_api.deps import settings_dep
from chartlens_core.config import ChartLensSettings, Environment
from chartlens_core.domain import utc_now

router = APIRouter(tags=["system"])


class ComponentVersions(BaseModel):
    api: str
    core: str
    engine: str
    pipeline: str


class HealthResponse(BaseModel):
    status: Literal["ok"]
    environment: Environment
    exchange: str
    methodology_hash: str
    versions: ComponentVersions
    server_time: datetime


@router.get("/health", response_model=HealthResponse)
def health(settings: Annotated[ChartLensSettings, Depends(settings_dep)]) -> HealthResponse:
    """Liveness plus the provenance a client needs to label what it is showing."""
    return HealthResponse(
        status="ok",
        environment=settings.runtime.environment,
        exchange=settings.universe.exchange,
        methodology_hash=settings.methodology_hash(),
        versions=ComponentVersions(
            api=chartlens_api.__version__,
            core=chartlens_core.__version__,
            engine=chartlens_engine.__version__,
            pipeline=chartlens_pipeline.__version__,
        ),
        server_time=utc_now(),
    )
