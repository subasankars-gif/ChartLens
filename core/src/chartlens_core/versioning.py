"""Version stamps. Every persisted result carries one (spec §43)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from chartlens_core.domain import utc_now


class VersionStamp(BaseModel):
    """Everything needed to reproduce a result.

    ``data_version`` identifies the canonical daily dataset the result was computed
    from (defined in Milestone 3 as the hash of that dataset's manifest).
    ``methodology_hash`` comes from ``ChartLensSettings.methodology_hash()``.
    """

    model_config = ConfigDict(frozen=True)

    engine_version: str
    pipeline_version: str
    data_version: str
    methodology_hash: str
    created_at: datetime = Field(default_factory=utc_now)
