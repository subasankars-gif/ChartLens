from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from chartlens_core.config import CONFIG_FILE_ENV, ChartLensSettings, resolve_config_file


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run every test from an empty directory with no CHARTLENS_* env leaking in."""
    import os

    for key in list(os.environ):
        if key.startswith("CHARTLENS_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)


def test_defaults_reflect_locked_phase1_decisions() -> None:
    s = ChartLensSettings()
    assert s.universe.exchange == "NSE"
    assert s.universe.series == ("EQ", "BE")
    assert s.universe.history_target_years == 20
    assert s.adjustment.splits and s.adjustment.bonus
    assert not s.adjustment.dividends
    assert s.storage.backend == "local"


def test_env_overrides_nested_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHARTLENS_STORAGE__BACKEND", "gcs")
    monkeypatch.setenv("CHARTLENS_STORAGE__GCS_BUCKET", "chartlens-data")
    s = ChartLensSettings()
    assert s.storage.backend == "gcs"
    assert s.storage.gcs_bucket == "chartlens-data"


def test_toml_file_is_found_by_walking_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "chartlens.toml").write_text('[universe]\nseries = ["EQ"]\n')
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert resolve_config_file() == (tmp_path / "config" / "chartlens.toml").resolve()
    assert ChartLensSettings().universe.series == ("EQ",)


def test_env_beats_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "chartlens.toml").write_text('[runtime]\nlog_level = "DEBUG"\n')
    monkeypatch.setenv("CHARTLENS_RUNTIME__LOG_LEVEL", "ERROR")
    assert ChartLensSettings().runtime.log_level == "ERROR"


def test_explicit_config_file_must_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONFIG_FILE_ENV, "/nonexistent/chartlens.toml")
    with pytest.raises(FileNotFoundError):
        resolve_config_file()


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "chartlens.toml").write_text('[universe]\nserise = ["EQ"]\n')
    with pytest.raises(ValidationError):
        ChartLensSettings()


def test_methodology_hash_is_stable() -> None:
    assert ChartLensSettings().methodology_hash() == ChartLensSettings().methodology_hash()
    assert len(ChartLensSettings().methodology_hash()) == 12


def test_methodology_hash_ignores_infrastructure(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = ChartLensSettings().methodology_hash()
    monkeypatch.setenv("CHARTLENS_STORAGE__BACKEND", "gcs")
    monkeypatch.setenv("CHARTLENS_RUNTIME__LOG_LEVEL", "DEBUG")
    assert ChartLensSettings().methodology_hash() == baseline


def test_methodology_hash_changes_with_methodology(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = ChartLensSettings().methodology_hash()
    monkeypatch.setenv("CHARTLENS_ADJUSTMENT__DIVIDENDS", "true")
    assert ChartLensSettings().methodology_hash() != baseline


def test_repo_config_file_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    repo_config = Path(__file__).resolve().parents[2] / "config" / "chartlens.toml"
    monkeypatch.setenv(CONFIG_FILE_ENV, str(repo_config))
    shipped = ChartLensSettings()
    # The shipped file documents the defaults; it must not silently drift from them.
    assert shipped.methodology() == ChartLensSettings.model_construct().methodology()
