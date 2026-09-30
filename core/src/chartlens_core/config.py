"""Centralised configuration.

Two kinds of settings live here, and the distinction matters for reproducibility:

* **Infrastructure** (``runtime``, ``storage``, ``firestore``, ``api``): where things
  run and where data lives. Changing these never changes a result.
* **Methodology** (``universe``, ``weekly``, ``adjustment``, ``data_quality`` and,
  from Phase 2, every analysis threshold): anything that can change a number
  ChartLens produces. These are hashed into ``methodology_hash`` and stamped on
  every output, so a silent methodology change is impossible (spec §57 rule 13).

Precedence (highest first): explicit kwargs → environment (``CHARTLENS_`` prefix,
``__`` for nesting) → TOML file → defaults below.

The TOML file is ``$CHARTLENS_CONFIG_FILE`` if set, otherwise ``config/chartlens.toml``
found by walking up from the current directory.
"""

from __future__ import annotations

import hashlib
import json
import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

CONFIG_FILE_ENV = "CHARTLENS_CONFIG_FILE"
DEFAULT_CONFIG_RELPATH = Path("config") / "chartlens.toml"


class Environment(StrEnum):
    LOCAL = "local"
    CI = "ci"
    PROD = "prod"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- infrastructure


class RuntimeConfig(_Section):
    environment: Environment = Environment.LOCAL
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = False


class StorageConfig(_Section):
    backend: Literal["local", "gcs"] = "local"
    local_root: Path = Path(".data")
    gcs_bucket: str | None = None


class FirestoreConfig(_Section):
    project_id: str | None = None
    emulator_host: str | None = None


class ApiConfig(_Section):
    cors_origins: tuple[str, ...] = ("http://localhost:3000",)


# --------------------------------------------------------------------------- methodology


class UniverseConfig(_Section):
    exchange: str = "NSE"
    series: tuple[str, ...] = ("EQ", "BE")
    history_target_years: int = Field(default=20, ge=1)
    """Target, not a requirement: every security keeps whatever history actually exists."""


class WeeklyConfig(_Section):
    grouping: Literal["iso_week"] = "iso_week"
    """Trading sessions are bucketed by ISO week (Mon–Sun). See ADR-0004."""
    week_end_label: Literal["last_session"] = "last_session"
    """week_end_date is the last *actual* session of the week, not the calendar Friday."""


class AdjustmentConfig(_Section):
    splits: bool = True
    bonus: bool = True
    rights: bool = True
    """Rights issues adjusted with the theoretical ex-rights price (TERP) factor."""
    dividends: bool = False
    """Charts show prices as traded; dividends are not adjusted by default. See ADR-0005."""


class DataQualityConfig(_Section):
    max_unexplained_move: float = Field(default=0.25, gt=0)
    """Close-to-close move (as a fraction) that is flagged when no corporate action explains it."""
    max_missing_session_ratio: float = Field(default=0.02, ge=0, le=1)
    """Fraction of expected sessions that may be missing before status degrades to WARN."""


class ChartLensSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CHARTLENS_",
        env_nested_delimiter="__",
        extra="forbid",
        frozen=True,
    )

    runtime: RuntimeConfig = RuntimeConfig()
    storage: StorageConfig = StorageConfig()
    firestore: FirestoreConfig = FirestoreConfig()
    api: ApiConfig = ApiConfig()

    universe: UniverseConfig = UniverseConfig()
    weekly: WeeklyConfig = WeeklyConfig()
    adjustment: AdjustmentConfig = AdjustmentConfig()
    data_quality: DataQualityConfig = DataQualityConfig()

    METHODOLOGY_SECTIONS: ClassVar[tuple[str, ...]] = (
        "universe",
        "weekly",
        "adjustment",
        "data_quality",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        toml_path = resolve_config_file()
        if toml_path is not None:
            sources.append(TomlConfigSettingsSource(settings_cls, toml_file=toml_path))
        return tuple(sources)

    def methodology(self) -> dict[str, object]:
        """The subset of settings that can change a computed result."""
        return {
            name: getattr(self, name).model_dump(mode="json") for name in self.METHODOLOGY_SECTIONS
        }

    def methodology_hash(self) -> str:
        """Stable 12-hex-char fingerprint of the methodology settings."""
        canonical = json.dumps(self.methodology(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def resolve_config_file(start: Path | None = None) -> Path | None:
    """Locate the TOML config: the env var if set, else walk up to config/chartlens.toml."""
    explicit = os.environ.get(CONFIG_FILE_ENV)
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise FileNotFoundError(f"{CONFIG_FILE_ENV}={explicit} does not exist")
        return path
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / DEFAULT_CONFIG_RELPATH
        if candidate.is_file():
            return candidate
    return None


@lru_cache(maxsize=1)
def get_settings() -> ChartLensSettings:
    """Process-wide settings singleton. Tests should construct ChartLensSettings directly."""
    return ChartLensSettings()
