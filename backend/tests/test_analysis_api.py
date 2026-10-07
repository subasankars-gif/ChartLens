"""The analysis API (ADR-0026 Part 2) over a published schema-3 lake with long synthetic
histories, analysed by the real ANALYSIS stage: documents and sections served exactly as
stored, event retrieval by explicit predicates with a content-hash-bound cursor, and
every refusal."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from analysis_lake import Spec, build, read_bars, republish, with_new_close
from chartlens_jobs.analysis_stage import AnalysisStage, StoreSpec
from fakes import FakeVerifier, MemoryAppState
from fastapi.testclient import TestClient

from chartlens_api.deps import appstate_dep, settings_dep, snapshots_dep, verifier_dep
from chartlens_api.lake import SnapshotProvider
from chartlens_api.main import create_app
from chartlens_core.canonical import canonical_json
from chartlens_core.config import ApiConfig, ChartLensSettings
from chartlens_engine.analysis import analysis_version
from chartlens_engine.explain import explain_version
from chartlens_pipeline.analysis_store import read_document, read_events
from chartlens_pipeline.serving import ServingPublisher
from chartlens_pipeline.storage import DataLakeLayout, LocalObjectStore

ADMIN = {"Authorization": "Bearer admin-uid:boss@example.com"}
SPECS = [
    Spec("SEC-A", seed=1, weeks=700),
    Spec("SEC-B", seed=2, weeks=500),
    Spec("SEC-X", seed=5, status="NOT_USABLE"),
]


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def analyse_and_publish(store: LocalObjectStore) -> dict[str, Any]:
    settings = ChartLensSettings()
    AnalysisStage(settings, "NSE", StoreSpec("local", root=str(store.root)), workers=1).run()
    published: dict[str, Any] = ServingPublisher(
        settings,
        SimpleNamespace(exchange_code="NSE"),  # type: ignore[arg-type]
        store,
        expected_analysis_version=analysis_version(settings.analysis),
        expected_explain_version=explain_version(),
    ).run()
    return published


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    store = LocalObjectStore(Path(tmp_path_factory.mktemp("lake")))
    build(store, SPECS)
    published = analyse_and_publish(store)
    api = ChartLensSettings.model_construct(
        api=ApiConfig(firebase_project_id="demo-test", admin_emails=("boss@example.com",))
    )
    clock = Clock()
    snapshots = SnapshotProvider(store, "NSE", refresh_seconds=60, clock=clock)
    app = create_app(api)
    appstate = MemoryAppState()
    app.dependency_overrides.update(
        {
            settings_dep: lambda: api,
            verifier_dep: FakeVerifier,
            appstate_dep: lambda: appstate,
            snapshots_dep: lambda: snapshots,
        }
    )
    return {
        "client": TestClient(app),
        "store": store,
        "published": published,
        "snapshots": snapshots,
        "clock": clock,
    }


def get(env: dict[str, Any], path: str, **params: Any) -> Any:
    return env["client"].get(f"/api/v1{path}", params=params, headers=ADMIN)


# ----------------------------------------------------------------------------- documents


def test_the_whole_document_is_served_exactly_as_stored(env: dict[str, Any]) -> None:
    r = get(env, "/securities/SEC-A/analysis")
    assert r.status_code == 200
    body = r.json()
    entry = env["snapshots"].get().analysis_entries["SEC-A"]
    stored = read_document(env["store"], "NSE", entry.document_sha256)
    # ADR-0026 §2.3: every value, and every number's canonical text, as stored.
    assert canonical_json(body["document"]) == stored
    assert r.content.find(stored[1:-1]) > 0  # the stored bytes appear verbatim in the body
    env_ = body["envelope"]
    published = env["published"]
    assert env_["document_sha256"] == entry.document_sha256
    assert env_["meta_version"] == published["meta_version"]
    assert env_["analysis_version"] == published["analysis"]["analysis_version"]
    assert env_["analysis_methodology_hash"] == published["analysis"]["analysis_methodology_hash"]
    assert env_["weekly_version"] == published["versions"]["weekly_version"]
    assert env_["sections"] == list(json.loads(stored))


def test_sections_are_whole_stored_sections(env: dict[str, Any]) -> None:
    full = get(env, "/securities/SEC-A/analysis").json()["document"]
    r = get(env, "/securities/SEC-A/analysis", sections="current,patterns,levels")
    assert r.status_code == 200
    doc = r.json()["document"]
    assert set(doc) == {"current", "patterns", "levels"}
    for name in doc:
        assert canonical_json(doc[name]) == canonical_json(full[name])


def test_unknown_sections_are_refused_not_ignored(env: dict[str, Any]) -> None:
    r = get(env, "/securities/SEC-A/analysis", sections="patterns,ranking")
    assert r.status_code == 400 and "ranking" in r.json()["detail"]


def test_refusals(env: dict[str, Any]) -> None:
    assert get(env, "/securities/SEC-A/analysis", as_of="2015-01-02").status_code == 400
    assert get(env, "/securities/NOPE/analysis").status_code == 404
    r = get(env, "/securities/SEC-X/analysis")  # published, but NOT_USABLE: not analysed
    assert r.status_code == 404 and r.json()["detail"]["code"] == "not_analysed"
    assert env["client"].get("/api/v1/securities/SEC-A/analysis").status_code == 401


def test_status_reports_the_analysis_summary(env: dict[str, Any]) -> None:
    status = get(env, "/system/status").json()
    assert status["schema_version"] == 4
    assert status["analysis"]["securities"] == 2
    assert (
        status["analysis"]["analysis_set_hash"] == env["published"]["analysis"]["analysis_set_hash"]
    )


# ----------------------------------------------------------------------------- events


def all_rows(env: dict[str, Any], sid: str, dataset: str) -> list[dict[str, Any]]:
    entry = env["snapshots"].get().analysis_entries[sid]
    rows, _ = read_events(env["store"], "NSE", dataset, entry.events[dataset].content_sha256)  # type: ignore[arg-type]
    return rows


def test_events_come_verbatim_in_stored_order(env: dict[str, Any]) -> None:
    stored = all_rows(env, "SEC-A", "level_breakouts")
    assert len(stored) > 10
    r = get(env, "/securities/SEC-A/breakout-events", source="level", limit=500)
    body = r.json()
    assert r.status_code == 200 and body["envelope"]["dataset"] == "level_breakouts"
    assert canonical_json(body["rows"]) == canonical_json(stored[:500])
    assert body["envelope"]["row_count"] == len(stored)


def test_predicates_select_by_stored_fields_only(env: dict[str, Any]) -> None:
    stored = all_rows(env, "SEC-A", "level_breakouts")
    mid = stored[len(stored) // 2]["bar_date"]
    r = get(
        env,
        "/securities/SEC-A/breakout-events",
        source="level",
        **{"from": mid},
        direction="BREAKDOWN",
        limit=500,
    )
    expected = [x for x in stored if x["bar_date"] >= mid and x["direction"] == "BREAKDOWN"]
    assert r.json()["rows"] == expected


def test_paging_walks_the_stored_order_exactly(env: dict[str, Any]) -> None:
    stored = all_rows(env, "SEC-A", "level_breakouts")
    seen: list[dict[str, Any]] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"source": "level", "limit": 7}
        if cursor:
            params["cursor"] = cursor
        body = get(env, "/securities/SEC-A/breakout-events", **params).json()
        seen += body["rows"]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == stored


def test_a_cursor_is_bound_to_its_predicates(env: dict[str, Any]) -> None:
    first = get(env, "/securities/SEC-A/breakout-events", source="level", limit=3).json()
    r = get(
        env,
        "/securities/SEC-A/breakout-events",
        source="level",
        direction="BREAKOUT",
        cursor=first["next_cursor"],
    )
    assert r.status_code == 400
    assert (
        get(env, "/securities/SEC-A/breakout-events", source="level", cursor="x!").status_code
        == 400
    )


def test_event_refusals(env: dict[str, Any]) -> None:
    path = "/securities/SEC-A/breakout-events"
    assert get(env, path).status_code == 422  # source is required: never a merged stream
    assert get(env, path, source="both").status_code == 422
    assert get(env, path, source="level", pattern_type="DOUBLE_TOP").status_code == 400
    assert get(env, path, source="pattern", level_source_type="SWING").status_code == 400
    assert get(env, path, source="level", limit=501).status_code == 422
    assert get(env, path, source="level", as_of="2015-01-02").status_code == 400
    assert get(env, "/securities/SEC-X/breakout-events", source="level").status_code == 404


def test_a_cursor_from_another_snapshot_is_a_conflict(tmp_path: Path) -> None:
    """Page 1 from snapshot A and page 2 from snapshot B must never be presented as one
    result: the cursor carries the dataset's content hash, and B's differs."""
    store = LocalObjectStore(tmp_path / "lake")
    build(store, SPECS[:2])
    analyse_and_publish(store)
    clock = Clock()
    snapshots = SnapshotProvider(store, "NSE", refresh_seconds=60, clock=clock)
    api = ChartLensSettings.model_construct(
        api=ApiConfig(firebase_project_id="demo-test", admin_emails=("boss@example.com",))
    )
    app = create_app(api)
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
    path = "/api/v1/securities/SEC-A/breakout-events"
    first = client.get(path, params={"source": "level", "limit": 3}, headers=ADMIN).json()
    bars = read_bars(store, "SEC-A")
    republish(store, "wk-2", {"SEC-A": with_new_close(bars, bars[-1].close * 2)})
    analyse_and_publish(store)
    clock.t += 61  # the provider sees the new pointer
    r = client.get(
        path, params={"source": "level", "limit": 3, "cursor": first["next_cursor"]}, headers=ADMIN
    )
    assert r.status_code == 409


def test_a_corrupt_document_is_never_served(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "lake")
    build(store, SPECS[:1])
    analyse_and_publish(store)
    snapshots = SnapshotProvider(store, "NSE")
    entry = snapshots.get().analysis_entries["SEC-A"]
    key = DataLakeLayout.serving_analysis_key("NSE", entry.document_sha256)
    store.delete(key)
    store.put_immutable(key, b"\x1f\x8b corrupt")
    from chartlens_api.lake import LakeUnavailable

    with pytest.raises(LakeUnavailable):
        snapshots.analysis_document("SEC-A")


def test_a_schema_2_snapshot_says_it_has_no_analysis(tmp_path: Path) -> None:
    """Before the first schema-3 publication: explicit, never a silent empty result."""
    from chartlens_api.lake import NoAnalysisInSnapshot

    store = LocalObjectStore(tmp_path / "lake")
    build(store, SPECS[:1])
    analyse_and_publish(store)
    key = DataLakeLayout.serving_manifest_key("NSE")
    legacy = json.loads(store.get(key))
    legacy["schema_version"] = 2
    del legacy["analysis"]
    store.put(key, json.dumps(legacy).encode())
    snapshots = SnapshotProvider(store, "NSE")
    with pytest.raises(NoAnalysisInSnapshot):
        snapshots.analysis_document("SEC-A")


# ----------------------------------------------------------------------------- chart (ADR-0027 §3)


def test_chart_components_are_the_existing_payloads_from_one_snapshot(env: dict[str, Any]) -> None:
    sections = "current,patterns,levels"
    r = get(env, "/securities/SEC-A/chart", sections=sections)
    assert r.status_code == 200
    body = r.json()
    weekly = get(env, "/securities/SEC-A/weekly").json()
    analysis = get(env, "/securities/SEC-A/analysis", sections=sections).json()
    # Byte-identical components: no second model, nothing reinterpreted.
    assert canonical_json(body["weekly"]) == canonical_json(weekly)
    assert canonical_json(body["analysis"]) == canonical_json(analysis)
    # Every component names the snapshot it came from, and it is the same one.
    meta = env["published"]["meta_version"]
    assert body["meta_version"] == body["weekly"]["meta_version"] == meta
    assert body["analysis"]["envelope"]["meta_version"] == meta
    assert body["analysis_status"] == "analysed"
    assert body["data_as_of"] == body["weekly"]["as_of"]


def test_chart_defaults_and_history_view(env: dict[str, Any]) -> None:
    body = get(env, "/securities/SEC-A/chart").json()
    assert body["analysis"]["envelope"]["sections"] == [
        "identity",
        "versions",
        "current",
        "provenance",
    ]
    assert body["segments"] == "valid"
    history = get(env, "/securities/SEC-A/chart", segments="all").json()
    assert canonical_json(history["weekly"]) == canonical_json(
        get(env, "/securities/SEC-A/weekly", segments="all").json()
    )


def test_chart_of_an_unanalysed_security_is_bars_with_the_reason(env: dict[str, Any]) -> None:
    r = get(env, "/securities/SEC-X/chart", sections="patterns")
    assert r.status_code == 200
    body = r.json()
    assert body["analysis"] is None and body["analysis_status"] == "not_analysed"
    assert canonical_json(body["weekly"]) == canonical_json(
        get(env, "/securities/SEC-X/weekly").json()
    )


def test_chart_refusals(env: dict[str, Any]) -> None:
    assert get(env, "/securities/NOPE/chart").status_code == 404
    assert get(env, "/securities/SEC-A/chart", as_of="2015-01-02").status_code == 400
    r = get(env, "/securities/SEC-A/chart", sections="patterns,best_patterns")
    assert r.status_code == 400 and "best_patterns" in r.json()["detail"]
    assert get(env, "/securities/SEC-A/chart", segments="some").status_code == 422
    assert env["client"].get("/api/v1/securities/SEC-A/chart").status_code == 401


def _client(store: LocalObjectStore, snapshots: SnapshotProvider) -> TestClient:
    api = ChartLensSettings.model_construct(
        api=ApiConfig(firebase_project_id="demo-test", admin_emails=("boss@example.com",))
    )
    app = create_app(api)
    appstate = MemoryAppState()
    app.dependency_overrides.update(
        {
            settings_dep: lambda: api,
            verifier_dep: FakeVerifier,
            appstate_dep: lambda: appstate,
            snapshots_dep: lambda: snapshots,
        }
    )
    return TestClient(app)


def test_a_chart_never_mixes_snapshots(tmp_path: Path) -> None:
    """The provider holds snapshot A; B is published and A's document is cleaned up.
    The chart read fails on A's document, reloads, and reads bars *and* analysis from B
    together: never A's bars with B's analysis."""
    store = LocalObjectStore(tmp_path / "lake")
    build(store, SPECS[:1])
    first = analyse_and_publish(store)
    clock = Clock()
    snapshots = SnapshotProvider(store, "NSE", refresh_seconds=60, clock=clock)
    client = _client(store, snapshots)
    path = "/api/v1/securities/SEC-A/chart"
    a = client.get(path, headers=ADMIN).json()
    assert a["meta_version"] == first["meta_version"]
    old_doc = snapshots.get().analysis_entries["SEC-A"].document_sha256
    bars = read_bars(store, "SEC-A")
    republish(store, "wk-2", {"SEC-A": with_new_close(bars, bars[-1].close * 2)})
    second = analyse_and_publish(store)
    store.delete(DataLakeLayout.serving_analysis_key("NSE", old_doc))
    snapshots._analysis.clear()  # pyright: ignore[reportPrivateUsage]
    b = client.get(path, headers=ADMIN).json()  # clock not advanced: the provider still holds A
    assert b["meta_version"] == second["meta_version"] != first["meta_version"]
    assert (
        b["weekly"]["meta_version"]
        == b["analysis"]["envelope"]["meta_version"]
        == b["meta_version"]
    )
    assert b["weekly"]["bars"][-1]["close"] != a["weekly"]["bars"][-1]["close"]


def test_a_schema_2_chart_is_bars_with_the_reason(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "lake")
    build(store, SPECS[:1])
    analyse_and_publish(store)
    key = DataLakeLayout.serving_manifest_key("NSE")
    legacy = json.loads(store.get(key))
    legacy["schema_version"] = 2
    del legacy["analysis"]
    store.put(key, json.dumps(legacy).encode())
    body = (
        _client(store, SnapshotProvider(store, "NSE"))
        .get("/api/v1/securities/SEC-A/chart", headers=ADMIN)
        .json()
    )
    assert body["analysis"] is None and body["analysis_status"] == "no_analysis_in_snapshot"
    assert body["weekly"]["bars"]
