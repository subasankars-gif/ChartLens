from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chartlens_core.config import get_settings
from chartlens_pipeline.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _local_lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHARTLENS_STORAGE__LOCAL_ROOT", str(tmp_path / "lake"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_info_reports_versions_and_methodology_hash() -> None:
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload["versions"]) == {"core", "pipeline"}
    assert len(payload["methodology_hash"]) == 12
    assert "identity" in payload["methodology"]


def test_backfill_dry_run_plans_expected_sessions_without_network(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    result = runner.invoke(
        app,
        [
            "backfill",
            "--start-date",
            "2026-09-21",
            "--end-date",
            "2026-09-27",
            "--dry-run",
            "--report-file",
            str(report),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Backfill plan (dry run)" in result.output
    payload = json.loads(report.read_text())
    assert payload["metrics"]["trading_sessions"] == 5
    assert set(payload["plan"].values()) == {"DOWNLOAD"}
    assert not (tmp_path / "lake" / "raw").exists()


def test_backfill_single_date_dry_run() -> None:
    result = runner.invoke(app, ["backfill", "--date", "2026-09-29", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Trading sessions: 1" in result.output


def test_holiday_only_range_has_no_sessions() -> None:
    result = runner.invoke(app, ["backfill", "--date", "2026-10-02", "--dry-run"])  # Gandhi Jayanti
    assert result.exit_code == 0 and "Trading sessions: 0" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["backfill", "--dry-run"],
        ["backfill", "--date", "2026-09-29", "--start-date", "2026-09-01", "--dry-run"],
        ["backfill", "--start-date", "2026-09-02", "--end-date", "2026-09-01", "--dry-run"],
        ["backfill", "--date", "29-09-2026", "--dry-run"],
    ],
)
def test_backfill_rejects_bad_arguments(args: list[str]) -> None:
    assert runner.invoke(app, args).exit_code != 0


def test_uncovered_calendar_year_fails_loudly() -> None:
    result = runner.invoke(app, ["backfill", "--date", "1999-01-04", "--dry-run"])
    assert result.exit_code != 0
    assert "no data for 1999" in str(result.exception)


def test_refresh_security_still_dry_run_only() -> None:
    assert runner.invoke(app, ["refresh-security", "RELIANCE", "--dry-run"]).exit_code == 0
    assert runner.invoke(app, ["refresh-security", "RELIANCE"]).exit_code == 3
