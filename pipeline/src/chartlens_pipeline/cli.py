"""``chartlens-pipeline`` command line — the entrypoint every batch job calls.

GitHub Actions workflows (and later Cloud Run Jobs) invoke these commands; they
never contain pipeline logic themselves (ADR-0007). Commands whose milestone has
not landed yet only support ``--dry-run``, which prints the resolved plan.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated

import typer

import chartlens_core
import chartlens_pipeline
from chartlens_core.config import get_settings, resolve_config_file
from chartlens_core.domain import JobType

app = typer.Typer(add_completion=False, no_args_is_help=True, help="ChartLens data pipeline")

DryRun = Annotated[bool, typer.Option("--dry-run", help="Print the resolved plan and exit.")]


def _emit(payload: dict[str, object]) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str))


def _not_yet(job: JobType, milestone: str, plan: dict[str, object], dry_run: bool) -> None:
    settings = get_settings()
    _emit(
        {
            "job_type": job,
            "dry_run": dry_run,
            "exchange": settings.universe.exchange,
            "methodology_hash": settings.methodology_hash(),
            **plan,
        }
    )
    if not dry_run:
        typer.echo(f"{job} is implemented in {milestone}; only --dry-run is available.", err=True)
        raise typer.Exit(code=3)


@app.command()
def info() -> None:
    """Show versions and the resolved configuration."""
    settings = get_settings()
    _emit(
        {
            "versions": {
                "core": chartlens_core.__version__,
                "pipeline": chartlens_pipeline.__version__,
            },
            "config_file": str(resolve_config_file() or "(defaults only)"),
            "environment": settings.runtime.environment,
            "storage_backend": settings.storage.backend,
            "methodology_hash": settings.methodology_hash(),
            "methodology": settings.methodology(),
        }
    )


@app.command("ingest-daily")
def ingest_daily(
    trade_date: Annotated[
        str | None, typer.Option(help="YYYY-MM-DD; default: latest session")
    ] = None,
    dry_run: DryRun = False,
) -> None:
    """Fetch, store and process one session's full-market file."""
    resolved = date.fromisoformat(trade_date) if trade_date else None
    _not_yet(JobType.DAILY_INCREMENTAL, "Milestone 2", {"trade_date": resolved}, dry_run)


@app.command()
def backfill(
    start: Annotated[str, typer.Option(help="YYYY-MM-DD")],
    end: Annotated[str, typer.Option(help="YYYY-MM-DD")],
    dry_run: DryRun = False,
) -> None:
    """Fetch and process a historical date range."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise typer.BadParameter("start must be on or before end")
    _not_yet(JobType.BACKFILL, "Milestone 2", {"start": first, "end": last}, dry_run)


@app.command("refresh-security")
def refresh_security(
    symbol: Annotated[str, typer.Argument(help="Exchange symbol, e.g. RELIANCE")],
    dry_run: DryRun = False,
) -> None:
    """Rebuild one security's canonical and weekly data."""
    _not_yet(JobType.SECURITY_REFRESH, "Milestone 7", {"symbol": symbol.upper()}, dry_run)


if __name__ == "__main__":  # pragma: no cover
    app()
