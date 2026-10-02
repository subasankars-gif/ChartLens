"""``chartlens-pipeline`` command line — the entrypoint every batch job calls.

GitHub Actions workflows (and later Cloud Run Jobs) invoke these commands; they
never contain pipeline logic themselves (ADR-0007).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Annotated

import typer

import chartlens_core
import chartlens_pipeline
from chartlens_core.config import ChartLensSettings, get_settings, resolve_config_file
from chartlens_core.domain import JobType
from chartlens_core.logs import configure_logging, log_event
from chartlens_pipeline.ingest import BackfillReport, IdentityInputsChanged, NothingToCatchUp

app = typer.Typer(add_completion=False, no_args_is_help=True, help="ChartLens data pipeline")

DryRun = Annotated[
    bool, typer.Option("--dry-run", help="Print the plan without downloading or writing.")
]
Exchange = Annotated[str, typer.Option(help="Exchange code")]


def _emit(payload: Mapping[str, object]) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str))


def _parse_day(value: str | None, name: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter(f"{name} must be YYYY-MM-DD, got {value!r}") from None


def _provider(settings: ChartLensSettings, exchange: str):  # type: ignore[no-untyped-def]
    from chartlens_pipeline.http import HttpFetcher
    from chartlens_pipeline.providers import get_provider

    return get_provider(exchange, settings, HttpFetcher(settings.http))


def _store(settings: ChartLensSettings):  # type: ignore[no-untyped-def]
    from chartlens_pipeline.storage import object_store_from_config

    return object_store_from_config(settings.storage)


def _service(settings: ChartLensSettings, exchange: str):  # type: ignore[no-untyped-def]
    from chartlens_pipeline.ingest import IngestionService

    return IngestionService(settings, _provider(settings, exchange), _store(settings))


def _write_report(report_file: str | None, payload: Mapping[str, object]) -> None:
    if report_file:
        Path(report_file).write_text(json.dumps(payload, indent=2, default=str))


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
    try:
        report = service.backfill(
            first, last, dry_run=dry_run, refetch=refetch, reprocess=reprocess
        )
    except IdentityInputsChanged as err:
        typer.echo(f"Refusing to run: {err}", err=True)
        raise typer.Exit(code=4) from None
    _finish_backfill(report, report_file)


def _finish_backfill(report: BackfillReport, report_file: str | None) -> None:
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
    trade_date: Annotated[
        str | None,
        typer.Option(help="YYYY-MM-DD: just this session. Default: catch up through today."),
    ] = None,
    exchange: Exchange = "NSE",
    dry_run: DryRun = False,
    report_file: Annotated[str | None, typer.Option(help="Also write the JSON report here")] = None,
) -> None:
    """The scheduled daily job. Without --trade-date, ingests every expected session since
    the latest ingested one through today, so a missed run leaves no hole."""
    if trade_date is not None:
        day = _parse_day(trade_date, "--trade-date")
        assert day
        backfill(day=day.isoformat(), exchange=exchange, dry_run=dry_run, report_file=report_file)
        return
    settings = get_settings()
    configure_logging(settings.runtime)
    try:
        report = _service(settings, exchange).catch_up(dry_run=dry_run)
    except IdentityInputsChanged as err:
        typer.echo(f"Refusing to run: {err}", err=True)
        raise typer.Exit(code=4) from None
    except NothingToCatchUp as err:
        typer.echo(f"Refusing to run: {err}", err=True)
        raise typer.Exit(code=2) from None
    _finish_backfill(report, report_file)


@app.command("reprocess-pending")
def reprocess_pending(exchange: Exchange = "NSE") -> None:
    """Retry every date that still has identity-pending quarantined rows."""
    settings = get_settings()
    configure_logging(settings.runtime)
    try:
        report = _service(settings, exchange).reprocess_pending()
    except IdentityInputsChanged as err:
        typer.echo(f"Refusing to run: {err}", err=True)
        raise typer.Exit(code=4) from None
    typer.echo(report.render())
    if report.metrics.errors:
        raise typer.Exit(code=2)


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


# ----------------------------------------------------------------------------- Milestone 3

ca_app = typer.Typer(no_args_is_help=True, help="Corporate-action feed (ADR-0011)")
app.add_typer(ca_app, name="corporate-actions")


@ca_app.command("fetch")
def ca_fetch(
    start: Annotated[str | None, typer.Option("--start-date", help="YYYY-MM-DD")] = None,
    end: Annotated[str | None, typer.Option("--end-date", help="YYYY-MM-DD")] = None,
    exchange: Exchange = "NSE",
    refetch: Annotated[
        bool, typer.Option("--refetch", help="Download every window again, not only live ones.")
    ] = False,
    report_file: Annotated[str | None, typer.Option(help="Also write the JSON report here")] = None,
) -> None:
    """Download the feed window by window and store each response immutably."""
    from datetime import timedelta

    from chartlens_pipeline.corporate_actions import CorporateActionStore

    settings = get_settings()
    configure_logging(settings.runtime)
    provider = _provider(settings, exchange)
    first = (
        _parse_day(start, "--start-date")
        or getattr(settings.providers, exchange.lower()).corporate_actions_first_month
    )
    last = _parse_day(end, "--end-date") or date.today() + timedelta(days=90)  # noqa: DTZ011
    store = CorporateActionStore(exchange, provider.corporate_actions, _store(settings))
    report = store.fetch(first, last, refetch=refetch)
    payload = {"start": first, "end": last, **report.__dict__}
    _emit(payload)
    _write_report(report_file, payload)
    if report.failed:
        raise typer.Exit(code=2)


@ca_app.command("summary")
def ca_summary(
    exchange: Exchange = "NSE",
    show: Annotated[int, typer.Option(help="Unrecognised subjects to list")] = 30,
) -> None:
    """Classify every stored record (no network): counts by class, unrecognised subjects."""
    from collections import Counter
    from datetime import timedelta

    from chartlens_pipeline.corporate_actions import CorporateActionStore

    settings = get_settings()
    provider = _provider(settings, exchange)
    source = provider.corporate_actions
    first = getattr(settings.providers, exchange.lower()).corporate_actions_first_month
    records = CorporateActionStore(exchange, source, _store(settings)).current_records(
        first,
        date.today() + timedelta(days=366),  # noqa: DTZ011
    )
    classes: Counter[str] = Counter()
    unrecognised: Counter[str] = Counter()
    for rec, _ in records.records:
        cls = source.interpret(rec.subject).action_class
        classes[str(cls)] += 1
        if cls == "UNRECOGNISED":
            unrecognised[rec.subject] += 1
    _emit(
        {
            "records": len(records.records),
            "rejected_by_parser": len(records.rejected),
            "windows_missing": len(records.windows_missing),
            "by_class": dict(classes.most_common()),
            "by_ex_year": records.by_year,
            "unrecognised": unrecognised.most_common(show),
            "grammar_version": source.grammar_version,
        }
    )


@app.command()
def adjust(
    exchange: Exchange = "NSE",
    report_file: Annotated[str | None, typer.Option(help="Also write the JSON report here")] = None,
) -> None:
    """Build the adjusted analytical dataset from stored canonical rows and feed versions.

    Exit 5 when a hard requirement fails (a new or worsened large gap, an override that
    resolves to no security): the report is written, the dataset is not published.
    """
    from dataclasses import asdict

    from chartlens_pipeline.adjust import AdjustmentService

    settings = get_settings()
    configure_logging(settings.runtime)
    result = AdjustmentService(settings, _provider(settings, exchange), _store(settings)).run()
    payload: dict[str, object] = {
        "adjustment_version": result.adjustment_version,
        "identity_version": result.identity_version,
        "data_end": result.data_end,
        "published": result.published,
        "unresolved_overrides": result.unresolved_overrides,
        "counts": result.counts,
        "discontinuity": asdict(result.report),
    }
    _emit(payload)
    _write_report(report_file, payload)
    if not result.published:
        raise typer.Exit(code=5)


def _lookup_ids(store, exchange: str, symbol: str | None, isin: str | None) -> set[str]:  # type: ignore[no-untyped-def]
    import pyarrow as pa
    import pyarrow.parquet as pq

    from chartlens_pipeline.storage import DataLakeLayout

    history = pq.read_table(
        pa.BufferReader(store.get(DataLakeLayout.identifier_history_key(exchange)))
    ).to_pylist()
    wanted = {
        ("SYMBOL", symbol.upper()) if symbol else None,
        ("ISIN", isin.upper()) if isin else None,
    }
    return {
        r["security_id"] for r in history if (r["identifier_type"], r["identifier_value"]) in wanted
    }


@app.command("adjustment-report")
def adjustment_report(
    symbol: Annotated[str | None, typer.Option(help="Current or past symbol")] = None,
    isin: Annotated[str | None, typer.Option(help="ISIN")] = None,
    exchange: Exchange = "NSE",
) -> None:
    """Every corporate-action event decided for a security, with factor and evidence."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from chartlens_pipeline.storage import DataLakeLayout

    settings = get_settings()
    store = _store(settings)
    ids = _lookup_ids(store, exchange, symbol, isin)
    if not ids:
        typer.echo("no matching security", err=True)
        raise typer.Exit(code=1)
    events = pq.read_table(
        pa.BufferReader(store.get(DataLakeLayout.adjustment_events_key(exchange)))
    ).to_pylist()
    _emit({sid: [e for e in events if e["security_id"] == sid] for sid in sorted(ids)})


@app.command("data-quality")
def data_quality(
    exchange: Exchange = "NSE",
    report_file: Annotated[
        str | None, typer.Option(help="Also write the JSON summary here")
    ] = None,
) -> None:
    """Assess the published adjusted dataset: findings, continuity breaks, usable_from."""
    from chartlens_pipeline.data_quality import AdjustedDataNotPublished, DataQualityService

    settings = get_settings()
    configure_logging(settings.runtime)
    try:
        result = DataQualityService(settings, _provider(settings, exchange), _store(settings)).run()
    except AdjustedDataNotPublished as err:
        typer.echo(f"No published adjusted dataset ({err}); run `adjust` first.", err=True)
        raise typer.Exit(code=5) from None
    payload = {
        "dq_version": result.dq_version,
        "adjustment_version": result.adjustment_version,
        **result.summary,
    }
    _emit(payload)
    _write_report(report_file, payload)


@app.command("identity-rebuild")
def identity_rebuild(exchange: Exchange = "NSE") -> None:
    """Re-resolve all stored history with the current identity inputs (ADR-0013)."""
    from dataclasses import asdict

    from chartlens_pipeline.identity_rebuild import IdentityRebuildService

    settings = get_settings()
    configure_logging(settings.runtime)
    report = IdentityRebuildService(settings, _provider(settings, exchange), _store(settings)).run()
    _emit(asdict(report))
    if report.errors:
        raise typer.Exit(code=2)


@app.command("security-quality")
def security_quality(
    symbol: Annotated[str | None, typer.Option(help="Current or past symbol")] = None,
    isin: Annotated[str | None, typer.Option(help="ISIN")] = None,
    exchange: Exchange = "NSE",
) -> None:
    """A security's data-quality status, usable_from and findings."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from chartlens_pipeline.storage import DataLakeLayout

    settings = get_settings()
    store = _store(settings)
    ids = _lookup_ids(store, exchange, symbol, isin)
    if not ids:
        typer.echo("no matching security", err=True)
        raise typer.Exit(code=1)

    def table(key: str) -> list[dict[str, object]]:
        return pq.read_table(pa.BufferReader(store.get(key))).to_pylist()

    statuses = table(DataLakeLayout.data_quality_status_key(exchange))
    findings = table(DataLakeLayout.data_quality_findings_key(exchange))
    _emit(
        {
            sid: {
                "status": next((s for s in statuses if s["security_id"] == sid), None),
                "findings": [f for f in findings if f["security_id"] == sid],
            }
            for sid in sorted(ids)
        }
    )


@app.command()
def weekly(
    exchange: Exchange = "NSE",
    report_file: Annotated[
        str | None, typer.Option(help="Also write the JSON summary here")
    ] = None,
) -> None:
    """Build weekly bars from the published adjusted dataset (ADR-0014)."""
    from chartlens_pipeline.weekly import WeeklyInputsNotReady, WeeklyService

    settings = get_settings()
    configure_logging(settings.runtime)
    try:
        result = WeeklyService(settings, _provider(settings, exchange), _store(settings)).run()
    except WeeklyInputsNotReady as err:
        typer.echo(f"Refusing to run: {err}", err=True)
        raise typer.Exit(code=5) from None
    _emit(result.summary)
    _write_report(report_file, result.summary)


def _run_store(settings: ChartLensSettings):  # type: ignore[no-untyped-def]
    """Firestore when configured (production, ADR-0018); otherwise None."""
    if not settings.firestore.project_id:
        return None
    from chartlens_pipeline.runs import FirestoreRunStore

    return FirestoreRunStore(settings.firestore.project_id)


@app.command("publish-serving")
def publish_serving(
    exchange: Exchange = "NSE",
    report_file: Annotated[
        str | None, typer.Option(help="Also write the JSON summary here")
    ] = None,
    run_id: Annotated[str | None, typer.Option(help="The production run publishing")] = None,
) -> None:
    """Publish the versioned serving snapshot the API reads (ADR-0016, ADR-0018).

    Exit 5 when the inputs are out of step, 6 when publication stopped before the pointer
    moved. Either way the live snapshot is unchanged.
    """
    from chartlens_pipeline.serving import (
        PublicationFailed,
        ServingInputsNotReady,
        ServingPublisher,
    )

    settings = get_settings()
    configure_logging(settings.runtime)
    publisher = ServingPublisher(
        settings,
        _provider(settings, exchange),
        _store(settings),
        history=_run_store(settings),
        run_id=run_id,
    )
    try:
        summary = publisher.run()
    except ServingInputsNotReady as err:
        typer.echo(f"Refusing to run: {err}", err=True)
        raise typer.Exit(code=5) from None
    except PublicationFailed as err:
        typer.echo(f"Not published: {err}", err=True)
        raise typer.Exit(code=6) from None
    _emit(summary)
    _write_report(report_file, summary)


@app.command("weekly-bars")
def weekly_bars(
    symbol: Annotated[str | None, typer.Option(help="Current or past symbol")] = None,
    isin: Annotated[str | None, typer.Option(help="ISIN")] = None,
    as_of: Annotated[
        str | None, typer.Option("--as-of", help="YYYY-MM-DD (default: the data's as_of)")
    ] = None,
    all_segments: Annotated[
        bool,
        typer.Option("--all-segments", help="Every continuity segment, not just the valid one"),
    ] = False,
    last: Annotated[int, typer.Option(help="Show only the last N bars (0 = all)")] = 12,
    exchange: Exchange = "NSE",
) -> None:
    """A security's weekly bars as known as of a date (point-in-time when earlier)."""
    from dataclasses import asdict

    from chartlens_pipeline.weekly import WeeklyReader

    settings = get_settings()
    store = _store(settings)
    ids = _lookup_ids(store, exchange, symbol, isin)
    if not ids:
        typer.echo("no matching security", err=True)
        raise typer.Exit(code=1)
    reader = WeeklyReader(_provider(settings, exchange), store)
    out: dict[str, object] = {}
    for sid in sorted(ids):
        series = reader.load(sid, _parse_day(as_of, "--as-of"), all_segments=all_segments)
        bars = series.bars[-last:] if last else series.bars
        out[sid] = {
            "as_of": series.as_of,
            "source": series.source,
            "segment_ids": series.segment_ids,
            "versions": series.versions,
            "bars_total": len(series.bars),
            "bars": [asdict(b) for b in bars],
        }
    _emit(out)


# ----------------------------------------------------------------------------- M7: tracked runs

_EXIT_REASONS: dict[str, dict[int, str]] = {
    "INGEST": {
        2: "ingestion did not complete: a session failed, or the lake needs a backfill",
        4: "identity inputs changed; run identity-rebuild first",
    },
    "CORPORATE_ACTIONS": {2: "some corporate-action feed windows could not be downloaded"},
    "ADJUSTMENT": {5: "hard requirement failed; adjusted dataset not published"},
    "DATA_QUALITY": {5: "no published adjusted dataset to assess"},
    "WEEKLY": {5: "weekly inputs are not ready"},
    "PUBLISH_SERVING": {
        5: "serving inputs are out of step; not published",
        6: "publication stopped before the pointer moved; the live snapshot is unchanged",
    },
}


def _production_stages(settings: ChartLensSettings, exchange: str, run_id: str, workdir: Path):  # type: ignore[no-untyped-def]
    """Each stage calls the existing command in this process and reads its report."""
    from chartlens_core.runs import SnapshotOutcome, Stage
    from chartlens_pipeline.production import StageFailed, StageResult

    def call(stage: Stage, command, **kwargs):  # type: ignore[no-untyped-def]
        report = workdir / f"{stage.lower()}.json"
        try:
            command(exchange=exchange, report_file=str(report), **kwargs)
        except typer.Exit as exc:
            if exc.exit_code:
                reason = _EXIT_REASONS.get(stage, {}).get(exc.exit_code, "the command failed")
                raise StageFailed(f"{reason} (exit {exc.exit_code})") from None
        return json.loads(report.read_text()) if report.exists() else {}

    def ingest() -> StageResult:
        r = call(Stage.INGEST, ingest_daily)
        m = r.get("metrics", {})
        return StageResult(
            records_processed=m.get("rows_accepted"),
            details={
                "sessions_ingested": m.get("dates_ingested"),
                "rows_quarantined": m.get("rows_quarantined"),
                "end": str(r.get("end")),
            },
        )

    def corporate_actions() -> StageResult:
        r = call(Stage.CORPORATE_ACTIONS, ca_fetch)
        return StageResult(
            records_processed=r.get("downloaded"),
            details={"windows": r.get("windows"), "unchanged": r.get("unchanged")},
        )

    def adjustment() -> StageResult:
        r = call(Stage.ADJUSTMENT, adjust)
        counts = r.get("counts") or {}
        return StageResult(
            records_processed=counts.get("rows"),
            version=r.get("adjustment_version"),
            details={"data_end": str(r.get("data_end"))},
        )

    def data_quality_stage() -> StageResult:
        r = call(Stage.DATA_QUALITY, data_quality)
        return StageResult(records_processed=r.get("securities"), version=r.get("dq_version"))

    def weekly_stage() -> StageResult:
        r = call(Stage.WEEKLY, weekly)
        return StageResult(
            records_processed=r.get("row_count"),
            version=r.get("weekly_version"),
            data_as_of=_parse_day(r.get("as_of"), "as_of"),
            details={"files_written": r.get("files_written")},
        )

    def publish() -> StageResult:
        r = call(Stage.PUBLISH_SERVING, publish_serving, run_id=run_id)
        return StageResult(
            records_processed=(r.get("counts") or {}).get("securities"),
            version=r.get("meta_version"),
            snapshot_outcome=SnapshotOutcome(r.get("outcome", "PUBLISHED")),
            details={"outcome": str(r.get("outcome"))},
        )

    return {
        Stage.INGEST: ingest,
        Stage.CORPORATE_ACTIONS: corporate_actions,
        Stage.ADJUSTMENT: adjustment,
        Stage.DATA_QUALITY: data_quality_stage,
        Stage.WEEKLY: weekly_stage,
        Stage.PUBLISH_SERVING: publish,
    }


def _tracking_store(settings: ChartLensSettings):  # type: ignore[no-untyped-def]
    store = _run_store(settings)
    if store is None:
        from chartlens_pipeline.runs import MemoryRunStore

        typer.echo("firestore.project_id is not set: this run is not recorded durably.", err=True)
        return MemoryRunStore()
    return store


@app.command()
def daily(
    run_id: Annotated[str, typer.Option(help="ChartLens run id (from the API, or gh-…)")],
    create: Annotated[
        bool, typer.Option("--create", help="Record a new run (scheduled or manual)")
    ] = False,
    trigger: Annotated[str, typer.Option(help="With --create: schedule or manual")] = "manual",
    requested_by: Annotated[str, typer.Option(help="With --create: who asked")] = "manual",
    github_run_id: Annotated[str | None, typer.Option(envvar="GITHUB_RUN_ID")] = None,
    github_run_attempt: Annotated[int | None, typer.Option(envvar="GITHUB_RUN_ATTEMPT")] = None,
    exchange: Exchange = "NSE",
) -> None:
    """The tracked production run (ADR-0018): ingest → corporate actions → adjust → data
    quality → weekly → publish, each stage recorded. Exit 1 if the run failed, 3 if it
    may not start (another run is active, or this one is already finished)."""
    import tempfile

    from chartlens_core.domain import utc_now
    from chartlens_core.runs import RunRecord, RunStatus, start_run, valid_run_id
    from chartlens_pipeline.production import ProductionRunner
    from chartlens_pipeline.runs import ActiveRunExists, RunNotClaimable

    settings = get_settings()
    configure_logging(settings.runtime)
    if not valid_run_id(run_id):
        typer.echo(f"Invalid run id {run_id!r}", err=True)
        raise typer.Exit(code=2)
    if trigger not in ("schedule", "manual"):
        raise typer.BadParameter("--trigger must be schedule or manual")
    store = _tracking_store(settings)
    now = utc_now()
    store.expire_lost(now)
    try:
        if create:
            fresh = RunRecord(
                run_id=run_id,
                trigger=trigger,  # type: ignore[arg-type]
                requested_by=requested_by,
                requested_at=now,
            )
            run = start_run(
                fresh, now, github_run_id=github_run_id, github_run_attempt=github_run_attempt
            )
            store.create(run, now, supersede_queued=True)
        else:
            run = store.claim(
                run_id, now, github_run_id=github_run_id, github_run_attempt=github_run_attempt
            )
    except ActiveRunExists as err:
        typer.echo(f"Not started: run {err.run.run_id} is {err.run.status}.", err=True)
        raise typer.Exit(code=3) from None
    except RunNotClaimable as err:
        typer.echo(f"Not started: {err}.", err=True)
        raise typer.Exit(code=3) from None

    with tempfile.TemporaryDirectory(prefix="chartlens-run-") as tmp:
        stages = _production_stages(settings, exchange, run_id, Path(tmp))
        result = ProductionRunner(store, run, stages).execute()
    _emit(
        {
            "run_id": result.run_id,
            "status": result.status,
            "snapshot_outcome": result.snapshot_outcome,
            "serving_version": result.serving_version,
            "data_as_of": result.data_as_of,
            "error_summary": result.error_summary,
        }
    )
    if result.status != RunStatus.SUCCEEDED:
        raise typer.Exit(code=1)


@app.command("run-finalize")
def run_finalize(
    run_id: Annotated[str, typer.Option(help="The run this workflow was executing")],
    outcome: Annotated[
        str, typer.Option(help="The GitHub job status: success, failure, cancelled")
    ],
    github_run_id: Annotated[str | None, typer.Option(envvar="GITHUB_RUN_ID")] = None,
) -> None:
    """Close the run if the workflow ended without recording it (killed, cancelled,
    failed before the run started). Touches only a run of this GitHub run (ADR-0018)."""
    from chartlens_core.domain import utc_now
    from chartlens_core.runs import RunStatus, valid_run_id

    settings = get_settings()
    configure_logging(settings.runtime)
    if not valid_run_id(run_id):
        raise typer.Exit(code=2)
    store = _tracking_store(settings)
    status = RunStatus.CANCELLED if outcome == "cancelled" else RunStatus.FAILED
    reason = {
        "cancelled": "the workflow was cancelled",
        "failure": "the workflow ended before the run could record its result",
    }.get(outcome, "the workflow ended without closing the run")
    ended = store.end_active(run_id, utc_now(), status, reason, github_run_id=github_run_id)
    _emit({"run_id": run_id, "closed": ended is not None, "status": ended and ended.status})


if __name__ == "__main__":  # pragma: no cover
    app()
