"""FastAPI dependencies. Tests override ``settings_dep`` via ``app.dependency_overrides``."""

from __future__ import annotations

from chartlens_core.config import ChartLensSettings, get_settings


def settings_dep() -> ChartLensSettings:
    return get_settings()
