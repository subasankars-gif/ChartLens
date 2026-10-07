"""``chartlens-jobs`` command line: the tracked production run and the ANALYSIS stage.

The production workflow calls ``daily`` (and ``run-finalize`` if the job ends without
closing its run). Every stage but ANALYSIS is an unchanged ``chartlens-pipeline``
command, called in this process (ADR-0018, ADR-0025).
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Annotated, Any

import typer

from chartlens_core.config import ChartLensSettings, get_settings
from chartlens_core.logs import configure_logging
from chartlens_core.runs import SnapshotOutcome, Stage
from chartlens_engine.analysis import analysis_version
from chartlens_jobs.analysis_stage import AnalysisStage, AnalysisStageFailed, StoreSpec
from chartlens_jobs.production import StageFailed, StageResult

app = typer.Typer(add_completion=False, no_args_is_help=True, help="ChartLens job layer")

Exchange = Annotated[str, typer.Option(help="Exchange code")]


def _emit(payload: Mapping[str, object]) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str))


def _day(value: object) -> date | None:
    return date.fromisoformat(str(value)) if value else None


# ----------------------------------------------------------------------------- ANALYSIS


@app.command()
def analysis(
    exchange: Exchange = "NSE",
    no_reuse: Annotated[
        bool,
        typer.Option(
            "--no-reuse", help="Recompute every security (recovery after a fix; never scheduled)"
        ),
    ] = False,
    workers: Annotated[int | None, typer.Option(help="Processes (default: settings)")] = None,
    write_root: Annotated[
        str | None,
        typer.Option(
            help="Rehearsal: read the configured lake, write artifacts and the manifest only "
            "under this local directory (read-only towards the lake)"
        ),
    ] = None,
    report_file: Annotated[str | None, typer.Option(help="Also write the JSON summary")] = None,
) -> None:
    """Analyse the analytical universe of the published weekly version (ADR-0025). Exit 7
    if the stage failed; no manifest is written then."""
    settings = get_settings()
    configure_logging(settings.runtime)
    spec = StoreSpec.from_settings(settings)
    if write_root is not None:
        if spec.kind != "gcs":
            raise typer.BadParameter("--write-root rehearses against a GCS lake")
        spec = StoreSpec("overlay", root=write_root, bucket=spec.bucket)
    try:
        summary = AnalysisStage(
            settings, exchange, spec, workers=workers, allow_reuse=not no_reuse
        ).run()
    except AnalysisStageFailed as err:
        typer.echo(f"Analysis failed: {err}", err=True)
        raise typer.Exit(code=7) from None
    payload: dict[str, Any] = {
        **summary.details,
        "analysis_version": summary.manifest.analysis_version,
        "weekly_version": summary.manifest.weekly_version,
        "seconds": summary.seconds,
        "sample": summary.sample,
    }
    _emit(payload)
    if report_file:
        Path(report_file).write_text(json.dumps(payload, indent=2, default=str))


# ----------------------------------------------------------------------------- tracked runs

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


def _production_stages(
    settings: ChartLensSettings, exchange: str, run_id: str, workdir: Path
) -> dict[Stage, Callable[[], StageResult]]:
    """Each pipeline stage calls its existing command in this process and reads its
    report; ANALYSIS runs the job layer's stage."""
    from chartlens_pipeline import cli as pipeline

    def call(stage: Stage, command: Callable[..., None], **kwargs: Any) -> dict[str, Any]:
        report = workdir / f"{stage.lower()}.json"
        try:
            command(exchange=exchange, report_file=str(report), **kwargs)
        except typer.Exit as exc:
            if exc.exit_code:
                reason = _EXIT_REASONS.get(stage, {}).get(exc.exit_code, "the command failed")
                raise StageFailed(f"{reason} (exit {exc.exit_code})") from None
        loaded: dict[str, Any] = json.loads(report.read_text()) if report.exists() else {}
        return loaded

    def ingest() -> StageResult:
        r = call(Stage.INGEST, pipeline.ingest_daily)
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
        r = call(Stage.CORPORATE_ACTIONS, pipeline.ca_fetch)
        return StageResult(
            records_processed=r.get("downloaded"),
            details={"windows": r.get("windows"), "unchanged": r.get("unchanged")},
        )

    def adjustment() -> StageResult:
        r = call(Stage.ADJUSTMENT, pipeline.adjust)
        counts = r.get("counts") or {}
        return StageResult(
            records_processed=counts.get("rows"),
            version=r.get("adjustment_version"),
            details={"data_end": str(r.get("data_end"))},
        )

    def data_quality_stage() -> StageResult:
        r = call(Stage.DATA_QUALITY, pipeline.data_quality)
        return StageResult(records_processed=r.get("securities"), version=r.get("dq_version"))

    def weekly_stage() -> StageResult:
        r = call(Stage.WEEKLY, pipeline.weekly)
        return StageResult(
            records_processed=r.get("row_count"),
            version=r.get("weekly_version"),
            data_as_of=_day(r.get("as_of")),
            details={"files_written": r.get("files_written")},
        )

    def analysis_stage() -> StageResult:
        try:
            summary = AnalysisStage(settings, exchange, StoreSpec.from_settings(settings)).run()
        except AnalysisStageFailed as exc:
            raise StageFailed(str(exc)) from None
        return StageResult(
            records_processed=len(summary.manifest.universe),
            version=summary.manifest.analysis_version,
            details=summary.details,
        )

    def publish() -> StageResult:
        r = call(
            Stage.PUBLISH_SERVING,
            pipeline.publish_serving,
            run_id=run_id,
            analysis_version=analysis_version(settings.analysis),
        )
        return StageResult(
            records_processed=(r.get("counts") or {}).get("securities"),
            version=r.get("meta_version"),
            snapshot_outcome=SnapshotOutcome(r.get("outcome", "PUBLISHED")),
            details={
                "outcome": str(r.get("outcome")),
                **{f"verify_{k}": v for k, v in (r.get("verification") or {}).items()},
            },
        )

    return {
        Stage.INGEST: ingest,
        Stage.CORPORATE_ACTIONS: corporate_actions,
        Stage.ADJUSTMENT: adjustment,
        Stage.DATA_QUALITY: data_quality_stage,
        Stage.WEEKLY: weekly_stage,
        Stage.ANALYSIS: analysis_stage,
        Stage.PUBLISH_SERVING: publish,
    }


def _tracking_store(settings: ChartLensSettings):  # type: ignore[no-untyped-def]
    """Firestore when configured (production, ADR-0018); otherwise in memory."""
    if settings.firestore.project_id:
        from chartlens_pipeline.runs import FirestoreRunStore

        return FirestoreRunStore(settings.firestore.project_id)
    from chartlens_pipeline.runs import MemoryRunStore

    typer.echo("firestore.project_id is not set: this run is not recorded durably.", err=True)
    return MemoryRunStore()


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
    """The tracked production run (ADR-0018, ADR-0025): ingest → corporate actions →
    adjust → data quality → weekly → analysis → publish, each stage recorded. Exit 1 if
    the run failed, 3 if it may not start (another run is active, or this one is already
    finished)."""
    from chartlens_core.domain import utc_now
    from chartlens_core.runs import RunRecord, RunStatus, start_run, valid_run_id
    from chartlens_jobs.production import ProductionRunner
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
