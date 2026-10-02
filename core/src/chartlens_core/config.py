"""Centralised configuration.

Three kinds of settings live here, and the distinction matters for reproducibility:

* **Infrastructure** (``runtime``, ``storage``, ``firestore``, ``api``, ``http``,
  ``providers``): where things run and where data lives. Changing these never changes a
  result.
* **Data methodology** (``universe``, ``weekly``, ``adjustment``, ``data_quality``,
  ``identity``): anything that can change the data ChartLens produces. Hashed into
  ``methodology_hash`` and stamped on every data output, so a silent methodology change
  is impossible (spec §57 rule 13).
* **Analysis methodology** (``analysis``): every technical-analysis period, multiplier,
  tolerance and threshold. Hashed separately into ``analysis_methodology_hash``
  (ADR-0019), so an analysis change never rebuilds data and a data change is never
  mistaken for an analysis change.

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
from itertools import pairwise
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
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
    firebase_project_id: str | None = None
    """Firebase project whose ID tokens are accepted (ADR-0016). Unset: every protected
    route answers 503 — the API never runs without authentication."""
    admin_emails: tuple[str, ...] = ()
    """Verified Google emails that become admins on first sign-in. Everyone else starts
    pending until an admin enables them (the allowlist lives in Firestore)."""
    snapshot_refresh_seconds: float = Field(default=60.0, ge=0)
    """How often the API checks for a newer serving snapshot."""
    weekly_cache_size: int = Field(default=512, ge=0)
    # Production refresh (ADR-0018): the API starts the GitHub workflow as a GitHub App.
    github_repository: str | None = None
    """``owner/repo`` whose workflow runs the refresh."""
    github_workflow: str = "production-refresh.yml"
    github_ref: str = "main"
    github_app_id: str | None = None
    github_app_private_key: SecretStr | None = None
    """PEM, from Secret Manager. Never logged, never returned, never sent to a browser.
    Without it (or the app id or repository), refresh answers 503."""


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
    api_base_url: str = "https://www.nseindia.com"
    """Host of NSE's JSON APIs (corporate actions). Answers hosted runners (ADR-0011)."""
    corporate_actions_first_month: date = date(2006, 1, 1)
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
    analytical_instrument_types: tuple[str, ...] = ("EQUITY_SHARE",)
    """Instrument types analysed and scanned (decision 2026-10-02: equity shares only).
    Every ingested series (ETFs, rights entitlements...) stays in the canonical data; the
    exchange's identity policy assigns the type from the ISIN. ADR-0012."""


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
    validation_min_tolerance: float = Field(default=0.15, gt=0)
    """An applied factor is VERIFIED when |ln(ex-date gap / factor)| is within
    max(this, sigma_multiplier × the stock's robust overnight-gap sigma). ADR-0011."""
    validation_sigma_multiplier: float = Field(default=5.0, gt=0)
    validation_window: int = Field(default=250, ge=20)
    """Trailing rows used for the robust overnight-gap sigma (point-in-time: before the event)."""
    gap_report_threshold: float = Field(default=0.25, gt=0)
    """A large gap in the market-wide discontinuity report: |open / prev close - 1| above this."""


class DataQualityConfig(_Section):
    max_unexplained_move: float = Field(default=0.25, gt=0)
    """Adjusted close-to-close move (as a fraction) flagged for review when no corporate
    action explains it. A warning only: a large move may be real (ADR-0012)."""
    max_missing_session_ratio: float = Field(default=0.02, ge=0, le=1)
    """Fraction of expected sessions that may be missing before status degrades to WARN."""
    max_trading_gap_sessions: int = Field(default=65, ge=1)
    """A security absent for more than this many consecutive expected sessions (about three
    months) has no price discovery across the gap: a continuity break (ADR-0012)."""
    unexplained_gap_break: float = Field(default=0.5, gt=0)
    """An analytical-universe security whose adjusted overnight gap |open / previous close
    - 1| exceeds this, with no accepted corporate-action explanation, has a continuity break
    (UNEXPLAINED_PRICE_DISCONTINUITY). Never an inferred adjustment. Decision 2026-10-02."""
    unexplained_gap_min_reference_price: float = Field(default=2.0, ge=0)
    """The break rule applies only when the previous raw close is at least this (rupees):
    below it, tick-size moves dominate."""
    max_session_quarantine_ratio: float = Field(default=0.05, ge=0, le=1)
    """Share of a session's in-scope rows the parser may reject before the session is
    treated as QUARANTINED rather than ingested (the 2020-07-13 case rejected 100%)."""


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


# --------------------------------------------------------------------------- analysis
#
# Technical-analysis methodology (ADR-0019/0020). Hashed separately from the data
# methodology above (``analysis_methodology_hash``): changing an analysis threshold must
# never change weekly data or its versions.


class IndicatorConfig(_Section):
    """Weekly indicators (ADR-0020 §A). Every value is part of a definition."""

    sma_periods: tuple[int, ...] = (10, 20, 40, 50, 100, 200)
    """Simple moving averages. "10W", "20W", ... are aliases of these on weekly bars."""
    ema_periods: tuple[int, ...] = (10, 20, 50, 100, 200)
    rsi_period: int = Field(default=14, ge=2)
    """Wilder's RSI."""
    macd_fast: int = Field(default=12, ge=1)
    macd_slow: int = Field(default=26, ge=2)
    macd_signal: int = Field(default=9, ge=1)
    stochastic_k: int = Field(default=14, ge=1)
    """Look-back of raw %K."""
    stochastic_k_smoothing: int = Field(default=3, ge=1)
    """Slow %K = SMA of raw %K over this many bars."""
    stochastic_d: int = Field(default=3, ge=1)
    roc_period: int = Field(default=12, ge=1)
    atr_period: int = Field(default=14, ge=1)
    """Wilder's ATR."""
    bollinger_period: int = Field(default=20, ge=2)
    bollinger_k: float = Field(default=2.0, gt=0)
    """Band width in population standard deviations."""
    volume_sma_period: int = Field(default=20, ge=1)
    rvol_baseline: int = Field(default=20, ge=1)
    """RVOL[t] = volume[t] / mean(volume[t-n .. t-1]): the current week is never part of its
    own baseline."""
    volume_trend_short: int = Field(default=10, ge=1)
    volume_trend_long: int = Field(default=20, ge=2)
    volume_trend_band: float = Field(default=0.10, ge=0)
    """RISING / FALLING when the short volume SMA is beyond the long one by this fraction."""
    rvol_expansion: float = Field(default=1.5, gt=0)
    rvol_contraction: float = Field(default=0.67, gt=0)

    @model_validator(mode="after")
    def _coherent(self) -> IndicatorConfig:
        if self.macd_fast >= self.macd_slow:
            raise ValueError("macd_fast must be shorter than macd_slow")
        if self.volume_trend_short >= self.volume_trend_long:
            raise ValueError("volume_trend_short must be shorter than volume_trend_long")
        if self.rvol_contraction >= self.rvol_expansion:
            raise ValueError("rvol_contraction must be below rvol_expansion")
        if any(n < 1 for n in (*self.sma_periods, *self.ema_periods)):
            raise ValueError("moving-average periods must be positive")
        return self


SwingMethod = Literal["FRACTAL", "ATR", "PERCENT", "ZIGZAG"]
Sensitivity = Literal["MICRO", "MINOR", "INTERMEDIATE", "MAJOR"]
SENSITIVITIES: tuple[Sensitivity, ...] = ("MICRO", "MINOR", "INTERMEDIATE", "MAJOR")


class SensitivityScale(_Section):
    """One method's parameter at each sensitivity, strictly increasing MICRO → MAJOR."""

    MICRO: float = Field(gt=0)
    MINOR: float = Field(gt=0)
    INTERMEDIATE: float = Field(gt=0)
    MAJOR: float = Field(gt=0)

    @model_validator(mode="after")
    def _increasing(self) -> SensitivityScale:
        values = [self.MICRO, self.MINOR, self.INTERMEDIATE, self.MAJOR]
        if any(b <= a for a, b in pairwise(values)):
            raise ValueError("sensitivity parameters must increase from MICRO to MAJOR")
        return self

    def at(self, sensitivity: Sensitivity) -> float:
        value: float = getattr(self, sensitivity)
        return value


class SwingConfig(_Section):
    """Swing points (ADR-0020 §B). The primary method and sensitivity are configuration:
    structure, Fibonacci, divergence and patterns read "the primary swings", never a
    named method (K4)."""

    primary_method: SwingMethod = "ATR"
    primary_sensitivity: Sensitivity = "INTERMEDIATE"
    fractal_window: SensitivityScale = SensitivityScale(MICRO=1, MINOR=2, INTERMEDIATE=3, MAJOR=5)
    """FRACTAL: bars on each side a pivot must dominate (whole numbers)."""
    atr_multiple: SensitivityScale = SensitivityScale(
        MICRO=1.0, MINOR=2.0, INTERMEDIATE=3.0, MAJOR=5.0
    )
    """ATR: reversal needed to confirm a pivot, in ATR(14) at the pivot bar."""
    percent: SensitivityScale = SensitivityScale(MICRO=5, MINOR=10, INTERMEDIATE=15, MAJOR=25)
    """PERCENT: reversal from the pivot's high/low, in percent of the pivot price."""
    zigzag_percent: SensitivityScale = SensitivityScale(
        MICRO=5, MINOR=10, INTERMEDIATE=15, MAJOR=25
    )
    """ZIGZAG: classic ZigZag on closing prices, reversal in percent."""

    @model_validator(mode="after")
    def _whole_windows(self) -> SwingConfig:
        for s in SENSITIVITIES:
            if self.fractal_window.at(s) != int(self.fractal_window.at(s)):
                raise ValueError("fractal windows must be whole numbers of bars")
        return self


class AnalysisConfig(_Section):
    """Everything that can change a technical-analysis result (ADR-0019)."""

    indicators: IndicatorConfig = IndicatorConfig()
    swings: SwingConfig = SwingConfig()


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

    analysis: AnalysisConfig = AnalysisConfig()

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
        """Stable 12-hex-char fingerprint of the (data) methodology settings."""
        return _fingerprint(self.methodology())

    ANALYSIS_SECTIONS: ClassVar[tuple[str, ...]] = ("analysis",)

    def analysis_methodology(self) -> dict[str, object]:
        """The settings that can change a technical-analysis result (ADR-0019)."""
        return {
            name: getattr(self, name).model_dump(mode="json") for name in self.ANALYSIS_SECTIONS
        }

    def analysis_methodology_hash(self) -> str:
        """Fingerprint of the analysis settings only: an analysis threshold never changes
        ``methodology_hash``, and a data setting never changes this one."""
        return _fingerprint(self.analysis_methodology())


def _fingerprint(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
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
