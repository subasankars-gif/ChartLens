from __future__ import annotations

import json

from typer.testing import CliRunner

from chartlens_pipeline.cli import app

runner = CliRunner()


def test_info_reports_versions_and_methodology_hash() -> None:
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload["versions"]) == {"core", "pipeline"}
    assert len(payload["methodology_hash"]) == 12


def test_unimplemented_job_supports_dry_run_only() -> None:
    dry = runner.invoke(
        app, ["backfill", "--start", "2006-01-01", "--end", "2006-01-31", "--dry-run"]
    )
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.output)["job_type"] == "BACKFILL"

    real = runner.invoke(app, ["backfill", "--start", "2006-01-01", "--end", "2006-01-31"])
    assert real.exit_code == 3


def test_backfill_rejects_inverted_range() -> None:
    result = runner.invoke(
        app, ["backfill", "--start", "2020-01-02", "--end", "2020-01-01", "--dry-run"]
    )
    assert result.exit_code != 0
