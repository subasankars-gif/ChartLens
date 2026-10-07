"""The real API over a small test lake, for the frontend's end-to-end tests (ADR-0017).

Firebase is replaced by the unit-test fakes: a token "uid:email" is a verified sign-in,
and the allowlist lives in memory. ``admin@example.com`` is the bootstrap admin; anyone
else signs in as pending. Never used outside tests.

Production runs (ADR-0018) use the in-memory run store. "GitHub" is simulated: a
dispatched refresh is claimed and run here, stage by stage, a second per stage, and it
publishes nothing new (the test lake does not change), so it ends UNCHANGED.

usage: python scripts/e2e_api.py [PORT] [LAKE_DIR]   (default: a small test lake)
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "pipeline/tests"), str(ROOT / "backend/tests")]

import uvicorn  # noqa: E402
from chartlens_jobs.analysis_stage import AnalysisStage, StoreSpec  # noqa: E402
from chartlens_jobs.production import ProductionRunner, StageResult  # noqa: E402
from fakes import FakeVerifier, MemoryAppState  # noqa: E402
from test_adjust import SESSIONS, build_lake  # noqa: E402

from chartlens_api.deps import (  # noqa: E402
    appstate_dep,
    dispatcher_dep,
    runs_dep,
    settings_dep,
    snapshots_dep,
    verifier_dep,
)
from chartlens_api.lake import SnapshotProvider  # noqa: E402
from chartlens_api.main import create_app  # noqa: E402
from chartlens_core.config import ApiConfig, ChartLensSettings  # noqa: E402
from chartlens_core.domain import utc_now  # noqa: E402
from chartlens_core.runs import SnapshotOutcome, Stage  # noqa: E402
from chartlens_engine.analysis import analysis_version  # noqa: E402
from chartlens_pipeline.adjust import AdjustmentService, CorporateActionOverrides  # noqa: E402
from chartlens_pipeline.data_quality import DataQualityService  # noqa: E402
from chartlens_pipeline.identity import IdentityOverrides  # noqa: E402
from chartlens_pipeline.runs import MemoryRunStore  # noqa: E402
from chartlens_pipeline.serving import ServingPublisher, ServingSnapshot  # noqa: E402
from chartlens_pipeline.storage import LocalObjectStore  # noqa: E402
from chartlens_pipeline.weekly import WeeklyService  # noqa: E402


class SimulatedWorkflow:
    """Stands in for GitHub Actions: claims the dispatched run and runs fake stages."""

    def __init__(self, runs: MemoryRunStore, meta_version: str | None) -> None:
        self.runs = runs
        self.meta_version = meta_version

    def _stage(self) -> StageResult:
        time.sleep(1.0)
        return StageResult(records_processed=1)

    def _publish(self) -> StageResult:
        time.sleep(1.0)
        return StageResult(version=self.meta_version, snapshot_outcome=SnapshotOutcome.UNCHANGED)

    def _run(self, run_id: str) -> None:
        time.sleep(1.0)  # queued
        run = self.runs.claim(run_id, utc_now(), github_run_id="1", github_run_attempt=1)
        stages = {stage: self._stage for stage in Stage}
        stages[Stage.PUBLISH_SERVING] = self._publish
        ProductionRunner(self.runs, run, stages).execute()

    def dispatch(self, run_id: str) -> None:
        threading.Thread(target=self._run, args=(run_id,), daemon=True).start()


def main(port: int, lake: Path | None) -> None:
    if lake is not None:  # an existing lake with a published serving snapshot (real data)
        store = LocalObjectStore(lake)
    else:
        tmp = Path(tempfile.mkdtemp(prefix="chartlens-e2e-"))
        settings, provider, store, today = build_lake(tmp, ex=SESSIONS[17])
        AdjustmentService(
            settings, provider, store, overrides=CorporateActionOverrides(), today=today
        ).run()
        DataQualityService(settings, provider, store, identity_overrides=IdentityOverrides()).run()
        WeeklyService(settings, provider, store).run()
        AnalysisStage(settings, "NSE", StoreSpec("local", root=str(store.root)), workers=1).run()
        ServingPublisher(
            settings, provider, store, expected_analysis_version=analysis_version(settings.analysis)
        ).run()
    api = ChartLensSettings.model_construct(
        api=ApiConfig(
            firebase_project_id="e2e",
            admin_emails=("admin@example.com",),
            cors_origins=("http://localhost:3000", "http://127.0.0.1:3000"),
        )
    )
    app = create_app(api)
    snapshots = SnapshotProvider(store, "NSE")
    state = MemoryAppState()
    runs = MemoryRunStore()
    workflow = SimulatedWorkflow(runs, ServingSnapshot.current_version(store, "NSE"))
    app.dependency_overrides.update(
        {
            settings_dep: lambda: api,
            verifier_dep: FakeVerifier,
            appstate_dep: lambda: state,
            snapshots_dep: lambda: snapshots,
            runs_dep: lambda: runs,
            dispatcher_dep: lambda: workflow,
        }
    )
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main(
        int(sys.argv[1]) if len(sys.argv) > 1 else 8081,
        Path(sys.argv[2]) if len(sys.argv) > 2 else None,
    )
