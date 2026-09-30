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
from datetime import date
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


class HttpConfig(_Section):
    """Outbound HTTP to market-data sources. Retries are bounded (spec §30)."""

    timeout_seconds: float = Field(default=30.0, gt=0)
    max_attempts: int = Field(default=4, ge=1, le=10)
    backoff_seconds: float = Field(default=2.0, ge=0)
    """Base of exponential backoff: waits of b, 2b, 4b, ... between attempts."""
    max_retry_after_seconds: float = Field(default=60.0, ge=0)
    """Upper bound on honouring a server's Retry-After header."""
    min_request_interval_seconds: float = Field(default=0.5, ge=0)
    """Politeness delay between consecutive requests to the same host."""
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
    )


class NseProviderConfig(_Section):
    """Where NSE publishes daily bhavcopies. Established by the NSE probe (ADR-0008)."""

    archive_base_url: str = "https://nsearchives.nseindia.com"
    udiff_first_date: date = date(2024, 1, 1)
    """Earliest date for which the UDiFF bhavcopy is tried first (probe: present 2024-01-19,
    absent 2020-03-02). Before this, only the legacy file is requested."""
    legacy_last_date: date = date(2024, 7, 5)
    """Last date the legacy bhavcopy was published
    (probe: present 2024-07-05, absent 2024-07-08)."""


class ProvidersConfig(_Section):
    nse: NseProviderConfig = NseProviderConfig()


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


class IdentityConfig(_Section):
    """Security identity resolution (ADR-0009). Changing any value can change which
    security a row is assigned to, so this is methodology."""

    link_same_issuer_isin: bool = True
    """Link a new ISIN to an existing security when the exchange's identity policy says both
    ISINs belong to the same issuer AND symbol evidence connects them."""
    resolve_without_isin: bool = True
    """Resolve rows that carry no ISIN (NSE legacy files before ~2011) by symbol continuity."""
    max_symbol_gap_days: int = Field(default=45, ge=1)
    """Longest calendar-day gap across which two observations of a symbol count as continuous."""
    active_within_sessions: int = Field(default=20, ge=1)
    """A security traded within this many sessions of the latest ingested session is ACTIVE."""


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
    http: HttpConfig = HttpConfig()
    providers: ProvidersConfig = ProvidersConfig()

    universe: UniverseConfig = UniverseConfig()
    weekly: WeeklyConfig = WeeklyConfig()
    adjustment: AdjustmentConfig = AdjustmentConfig()
    data_quality: DataQualityConfig = DataQualityConfig()
    identity: IdentityConfig = IdentityConfig()

    METHODOLOGY_SECTIONS: ClassVar[tuple[str, ...]] = (
        "universe",
        "weekly",
        "adjustment",
        "data_quality",
        "identity",
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


def config_dir() -> Path | None:
    """Directory holding the active config file; versioned data files (identity overrides)
    live beside it. None when running on code defaults only."""
    path = resolve_config_file()
    return path.parent if path is not None else None


@lru_cache(maxsize=1)
def get_settings() -> ChartLensSettings:
    """Process-wide settings singleton. Tests should construct ChartLensSettings directly."""
    return ChartLensSettings()
