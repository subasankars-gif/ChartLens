"""Write the chart-layer test fixture: a real `/chart` response (every section) and the
security's pattern breakout events, from the API over a synthetic lake analysed by the
real ANALYSIS stage (jobs/tests/analysis_lake.py). The frontend's layer tests run the
adapters over it, so they see exactly what the engine publishes and the API serves.

The security has a continuity break, a forming last week, trendlines (one active),
patterns with measured moves, and Fibonacci structures. Only indicator series are
trimmed (to the ones the chart offers), to keep the file small; nothing else changes.

usage: uv run python scripts/layer_fixture.py [OUT]
"""

from __future__ import annotations

import gzip
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "jobs/tests"), str(ROOT / "backend/tests")]

from analysis_lake import Spec, build  # noqa: E402
from chartlens_jobs.analysis_stage import AnalysisStage, StoreSpec  # noqa: E402
from fakes import FakeVerifier, MemoryAppState  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from chartlens_api.deps import appstate_dep, settings_dep, snapshots_dep, verifier_dep  # noqa: E402
from chartlens_api.lake import SnapshotProvider  # noqa: E402
from chartlens_api.main import create_app  # noqa: E402
from chartlens_core.config import ApiConfig, ChartLensSettings  # noqa: E402
from chartlens_engine.analysis import analysis_version  # noqa: E402
from chartlens_engine.explain import explain_version  # noqa: E402
from chartlens_pipeline.serving import ServingPublisher  # noqa: E402
from chartlens_pipeline.storage import LocalObjectStore  # noqa: E402

SPEC = Spec("SEC-L", seed=6, weeks=700, break_at=250, forming=True)
KEEP_SERIES = {
    "sma_10",
    "sma_40",
    "rsi",
    "macd",
    "macd_signal",
    "bollinger_upper",
    "bollinger_middle",
    "bollinger_lower",
}
SECTIONS = ",".join(
    (
        "identity,versions,current,provenance",
        "indicators,swings,structure,levels,fibonacci,evidence,patterns",
    )
)
ADMIN = {"Authorization": "Bearer admin-uid:boss@example.com"}


def main(out: Path) -> None:
    store = LocalObjectStore(Path(tempfile.mkdtemp(prefix="chartlens-fixture-")))
    build(store, [SPEC])
    settings = ChartLensSettings()
    AnalysisStage(settings, "NSE", StoreSpec("local", root=str(store.root)), workers=1).run()
    ServingPublisher(
        settings,
        SimpleNamespace(exchange_code="NSE"),  # type: ignore[arg-type]
        store,
        expected_analysis_version=analysis_version(settings.analysis),
        expected_explain_version=explain_version(),
    ).run()
    api = ChartLensSettings.model_construct(
        api=ApiConfig(firebase_project_id="demo-test", admin_emails=("boss@example.com",))
    )
    app = create_app(api)
    snapshots = SnapshotProvider(store, "NSE")
    appstate = MemoryAppState()
    app.dependency_overrides.update(
        {
            settings_dep: lambda: api,
            verifier_dep: FakeVerifier,
            appstate_dep: lambda: appstate,
            snapshots_dep: lambda: snapshots,
        }
    )
    client = TestClient(app)
    sid = SPEC.security_id
    chart = client.get(
        f"/api/v1/securities/{sid}/chart",
        params={"sections": SECTIONS, "segments": "all"},
        headers=ADMIN,
    ).json()
    indicators = chart["analysis"]["document"]["indicators"]
    indicators["series"] = [s for s in indicators["series"] if s["name"] in KEEP_SERIES]
    breakouts = client.get(
        f"/api/v1/securities/{sid}/breakout-events",
        params={"source": "pattern", "limit": 500},
        headers=ADMIN,
    ).json()
    out.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"chart": chart, "breakouts": breakouts}, sort_keys=True).encode()
    out.write_bytes(gzip.compress(body, compresslevel=9, mtime=0))
    print(f"wrote {out} ({out.stat().st_size // 1024} KiB)")


if __name__ == "__main__":
    main(
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else ROOT / "frontend/src/lib/layers/__fixtures__/chart-SEC-L.json.gz"
    )
