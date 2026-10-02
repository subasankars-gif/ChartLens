"""The real API over a small test lake, for the frontend's end-to-end tests (ADR-0017).

Firebase is replaced by the unit-test fakes: a token "uid:email" is a verified sign-in,
and the allowlist lives in memory. ``admin@example.com`` is the bootstrap admin; anyone
else signs in as pending. Never used outside tests.

usage: python scripts/e2e_api.py [PORT] [LAKE_DIR]   (default: a small test lake)
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "pipeline/tests"), str(ROOT / "backend/tests")]

import uvicorn  # noqa: E402
from fakes import FakeVerifier, MemoryAppState  # noqa: E402
from test_adjust import SESSIONS, build_lake  # noqa: E402

from chartlens_api.deps import appstate_dep, settings_dep, snapshots_dep, verifier_dep  # noqa: E402
from chartlens_api.lake import SnapshotProvider  # noqa: E402
from chartlens_api.main import create_app  # noqa: E402
from chartlens_core.config import ApiConfig, ChartLensSettings  # noqa: E402
from chartlens_pipeline.adjust import AdjustmentService, CorporateActionOverrides  # noqa: E402
from chartlens_pipeline.data_quality import DataQualityService  # noqa: E402
from chartlens_pipeline.identity import IdentityOverrides  # noqa: E402
from chartlens_pipeline.serving import ServingPublisher  # noqa: E402
from chartlens_pipeline.storage import LocalObjectStore  # noqa: E402
from chartlens_pipeline.weekly import WeeklyService  # noqa: E402


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
        ServingPublisher(settings, provider, store).run()
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
    app.dependency_overrides.update(
        {
            settings_dep: lambda: api,
            verifier_dep: FakeVerifier,
            appstate_dep: lambda: state,
            snapshots_dep: lambda: snapshots,
        }
    )
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main(
        int(sys.argv[1]) if len(sys.argv) > 1 else 8081,
        Path(sys.argv[2]) if len(sys.argv) > 2 else None,
    )
