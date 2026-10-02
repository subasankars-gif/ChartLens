from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chartlens_core.config import get_settings
from chartlens_pipeline.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _local_lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CHARTLENS_STORAGE__LOCAL_ROOT", str(tmp_path / "lake"))
    get_settings.cache_clear()
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    # The CLI installs a handler on the runner's (now closed) stderr; restore logging.
    root.handlers[:], _ = handlers, root.setLevel(level)
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


def test_adjust_and_data_quality_commands_run_on_stored_data(tmp_path: Path) -> None:
    from test_adjust import build_lake

    build_lake(tmp_path)  # same lake root as the CLI's (see _local_lake)
    result = runner.invoke(app, ["adjust", "--report-file", str(tmp_path / "adj.json")])
    assert result.exit_code == 0, result.output
    report = json.loads((tmp_path / "adj.json").read_text())
    assert report["published"] and report["discontinuity"]["new_gaps_introduced"] == 0

    result = runner.invoke(app, ["data-quality", "--report-file", str(tmp_path / "dq.json")])
    assert result.exit_code == 0, result.output
    assert json.loads((tmp_path / "dq.json").read_text())["securities"] == 3

    result = runner.invoke(app, ["adjustment-report", "--symbol", "SPLITCO"])
    assert result.exit_code == 0, result.output
    (events,) = json.loads(result.stdout).values()
    assert events[0]["status"] == "VERIFIED" and events[0]["factor"] == "1/5"

    result = runner.invoke(app, ["security-quality", "--symbol", "DEMERCO"])
    assert result.exit_code == 0, result.output
    (entry,) = json.loads(result.stdout).values()
    assert entry["status"]["usable_from"] > entry["status"]["first_date"]

    result = runner.invoke(app, ["corporate-actions", "summary"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["records"] == 4


def test_data_quality_without_a_published_dataset_exits_5() -> None:
    result = runner.invoke(app, ["data-quality"])
    assert result.exit_code == 5


def test_weekly_refuses_without_a_published_adjusted_dataset() -> None:
    result = runner.invoke(app, ["weekly"])
    assert result.exit_code == 5
    assert "run `adjust`" in result.output
