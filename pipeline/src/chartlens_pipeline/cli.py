"""``chartlens-pipeline`` command line — the entrypoint every batch job calls.

GitHub Actions workflows (and later Cloud Run Jobs) invoke these commands; they
never contain pipeline logic themselves (ADR-0007).
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Annotated

import typer

import chartlens_core
import chartlens_pipeline
from chartlens_core.config import ChartLensSettings, get_settings, resolve_config_file
from chartlens_core.domain import JobType
from chartlens_core.logs import configure_logging, log_event

app = typer.Typer(add_completion=False, no_args_is_help=True, help="ChartLens data pipeline")

DryRun = Annotated[
    bool, typer.Option("--dry-run", help="Print the plan without downloading or writing.")
]
Exchange = Annotated[str, typer.Option(help="Exchange code")]


def _emit(payload: dict[str, object]) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str))


def _parse_day(value: str | None, name: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter(f"{name} must be YYYY-MM-DD, got {value!r}") from None


def _service(settings: ChartLensSettings, exchange: str):  # type: ignore[no-untyped-def]
    from chartlens_pipeline.http import HttpFetcher
    from chartlens_pipeline.ingest import IngestionService
    from chartlens_pipeline.providers import get_provider
    from chartlens_pipeline.storage import object_store_from_config

    fetcher = HttpFetcher(settings.http)
    provider = get_provider(exchange, settings, fetcher)
    return IngestionService(settings, provider, object_store_from_config(settings.storage))


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


@app.command()
def backfill(
    day: Annotated[str | None, typer.Option("--date", help="A single date, YYYY-MM-DD")] = None,
    start: Annotated[str | None, typer.Option("--start-date", "--start", help="YYYY-MM-DD")] = None,
    end: Annotated[str | None, typer.Option("--end-date", "--end", help="YYYY-MM-DD")] = None,
    exchange: Exchange = "NSE",
    dry_run: DryRun = False,
    refetch: Annotated[
        bool,
        typer.Option("--refetch", help="Download again even if stored (detects re-issued files)."),
    ] = False,
    reprocess: Annotated[
        bool, typer.Option("--reprocess", help="Re-parse stored files even if already ingested.")
    ] = False,
    report_file: Annotated[str | None, typer.Option(help="Also write the JSON report here")] = None,
) -> None:
    """Download, store, parse, resolve and write canonical daily bars for expected sessions.

    Dates are processed newest first. Already-ingested dates are skipped unless their
    inputs (parser version, identity rules, source versions) changed.
    """
    single = _parse_day(day, "--date")
    first = _parse_day(start, "--start-date")
    last = _parse_day(end, "--end-date")
    if single and (first or last):
        raise typer.BadParameter("use either --date or --start-date/--end-date, not both")
    if single:
        first = last = single
    if first is None or last is None:
        raise typer.BadParameter("give --date, or both --start-date and --end-date")
    if first > last:
        raise typer.BadParameter("--start-date must be on or before --end-date")

    settings = get_settings()
    configure_logging(settings.runtime)
    service = _service(settings, exchange)
    report = service.backfill(first, last, dry_run=dry_run, refetch=refetch, reprocess=reprocess)
    typer.echo(report.render())
    payload = {
        "job_type": JobType.BACKFILL,
        "job_id": report.job_id,
        "exchange": report.exchange,
        "start": report.start,
        "end": report.end,
        "dry_run": report.dry_run,
        "metrics": report.metrics.as_dict(),
        "plan": report.plan,
        "missing_sessions": report.missing_sessions,
        "duration_seconds": round(report.duration_seconds, 2),
    }
    if report_file:
        Path(report_file).write_text(json.dumps(payload, indent=2, default=str))
    if report.metrics.errors:
        raise typer.Exit(code=2)


@app.command("ingest-daily")
def ingest_daily(
    trade_date: Annotated[str | None, typer.Option(help="YYYY-MM-DD; default: today")] = None,
    exchange: Exchange = "NSE",
    dry_run: DryRun = False,
) -> None:
    """Ingest one session (the scheduled daily job). Same pipeline as ``backfill --date``."""
    day = _parse_day(trade_date, "--trade-date") or date.today()  # noqa: DTZ011 — exchange-local date
    backfill(day=day.isoformat(), exchange=exchange, dry_run=dry_run)


@app.command("reprocess-pending")
def reprocess_pending(exchange: Exchange = "NSE") -> None:
    """Retry every date that still has identity-pending quarantined rows."""
    settings = get_settings()
    configure_logging(settings.runtime)
    report = _service(settings, exchange).reprocess_pending()
    typer.echo(report.render())


@app.command("quarantine-report")
def quarantine_report(
    start: Annotated[str, typer.Option("--start-date", help="YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--end-date", help="YYYY-MM-DD")],
    reason: Annotated[str | None, typer.Option(help="Only this reason")] = None,
    exchange: Exchange = "NSE",
    limit: Annotated[int, typer.Option(help="Sample rows to show")] = 20,
) -> None:
    """Summarise quarantined rows for review (unresolved identities, invalid rows, …)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from chartlens_pipeline.storage import DataLakeLayout, object_store_from_config

    settings = get_settings()
    store = object_store_from_config(settings.storage)
    first, last = _parse_day(start, "--start-date"), _parse_day(end, "--end-date")
    assert first and last
    rows: list[dict[str, object]] = []
    for key in store.list(DataLakeLayout.quarantine_daily_prefix(exchange)):
        day = date.fromisoformat(key.rsplit("/", 1)[-1].removesuffix(".parquet"))
        if first <= day <= last:
            rows += pq.read_table(pa.BufferReader(store.get(key))).to_pylist()
    if reason:
        rows = [r for r in rows if r["reason"] == reason.upper()]
    counts: dict[str, int] = {}
    for r in rows:
        counts[str(r["reason"])] = counts.get(str(r["reason"]), 0) + 1
    sample = [
        {
            k: r[k]
            for k in ("trading_date", "reason", "symbol", "series", "isin", "detail", "source_id")
        }
        for r in rows[:limit]
    ]
    _emit({"rows": len(rows), "by_reason": dict(sorted(counts.items())), "sample": sample})


@app.command("security")
def security_lookup(
    symbol: Annotated[str | None, typer.Option(help="Current or past symbol")] = None,
    isin: Annotated[str | None, typer.Option(help="ISIN")] = None,
    exchange: Exchange = "NSE",
) -> None:
    """Show a security's identity and full identifier history."""
    settings = get_settings()
    service = _service(settings, exchange)
    master = service.master
    ids: set[str] = set()
    if isin:
        ids |= master.by_isin(isin.upper())
    if symbol:
        ids |= {
            s.security_id
            for s in master.spans()
            if s.identifier_type == "SYMBOL" and s.value == symbol.upper()
        }
    if not ids:
        typer.echo("no matching security", err=True)
        raise typer.Exit(code=1)
    securities, _ = master.to_tables()
    by_id = {r["security_id"]: r for r in securities.to_pylist()}
    _emit(
        {
            sid: {
                **by_id[sid],
                "identifier_history": [
                    {
                        "type": str(s.identifier_type),
                        "value": s.value,
                        "valid_from": s.valid_from,
                        "valid_to": s.valid_to,
                        "evidence": str(s.evidence),
                        "source_id": s.source_id,
                    }
                    for s in master.spans()
                    if s.security_id == sid
                ],
            }
            for sid in sorted(ids)
        }
    )


@app.command("calendar-derive")
def calendar_derive(
    start: Annotated[str, typer.Option("--start-date", help="YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--end-date", help="YYYY-MM-DD")],
    output: Annotated[str, typer.Option(help="Write derived TOML here")] = "calendar-derived.toml",
    exchange: Exchange = "NSE",
    workers: Annotated[int, typer.Option(min=1, max=8)] = 4,
) -> None:
    """Derive calendar data from which daily files the exchange actually published."""
    from chartlens_pipeline.calendar_derive import (
        ImplausibleCalendarError,
        derive_calendar,
        to_toml,
    )
    from chartlens_pipeline.http import HttpFetcher
    from chartlens_pipeline.providers import get_provider

    settings = get_settings()
    configure_logging(settings.runtime)
    log = logging.getLogger("chartlens.pipeline.calendar")

    def make_source():  # type: ignore[no-untyped-def]
        return get_provider(exchange, settings, HttpFetcher(settings.http)).daily_bars

    first, last = _parse_day(start, "--start-date"), _parse_day(end, "--end-date")
    assert first and last
    years = derive_calendar(
        make_source,
        first,
        last,
        workers=workers,
        progress=lambda n, total: log_event(log, "calendar.progress", checked=n, total=total),
    )
    summary = {
        y.year: {
            "sessions": y.sessions,
            "holidays": len(y.holidays),
            "special_sessions": [str(d) for d in y.special_sessions],
            "undetermined": [(str(d), why) for d, why in y.undetermined],
            "implausible": y.implausible(),
        }
        for y in years.values()
    }
    _emit({"output": output, "years": summary})
    try:
        Path(output).write_text(to_toml(years, date.today()))  # noqa: DTZ011 — label only
    except ImplausibleCalendarError as err:
        typer.echo(str(err), err=True)
        raise typer.Exit(code=5) from None
    if any(y.undetermined for y in years.values()):
        typer.echo("Some dates could not be determined; re-run for those years.", err=True)
        raise typer.Exit(code=4)


@app.command("refresh-security")
def refresh_security(
    symbol: Annotated[str, typer.Argument(help="Exchange symbol, e.g. RELIANCE")],
    dry_run: DryRun = False,
) -> None:
    """Rebuild one security's derived data (weekly bars, analysis). Milestone 7."""
    settings = get_settings()
    _emit(
        {
            "job_type": JobType.SECURITY_REFRESH,
            "dry_run": dry_run,
            "symbol": symbol.upper(),
            "methodology_hash": settings.methodology_hash(),
        }
    )
    if not dry_run:
        typer.echo(
            "refresh-security is implemented in Milestone 7; only --dry-run is available.", err=True
        )
        raise typer.Exit(code=3)


if __name__ == "__main__":  # pragma: no cover
    app()
