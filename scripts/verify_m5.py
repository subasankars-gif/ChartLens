"""Milestone 5 verification on real data (used by .github/workflows/m5-verify.yml).

Runs the real API (in process, authentication replaced by a fixed admin) over a local copy
of the lake after ``data-quality``, ``weekly`` and ``publish-serving``, and reports:
snapshot load time and memory, response times and sizes, version binding (every response
names the same ``meta_version``), and worked examples.

usage: python scripts/verify_m5.py LAKE_DIR OUT_DIR
"""

from __future__ import annotations

import json
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from chartlens_api.appstate import Role, User, UserNotFound, Watchlist
from chartlens_api.auth import Claims
from chartlens_api.deps import appstate_dep, settings_dep, snapshots_dep, verifier_dep
from chartlens_api.lake import SnapshotProvider
from chartlens_api.main import create_app
from chartlens_core.config import ApiConfig, ChartLensSettings
from chartlens_pipeline.storage import LocalObjectStore

H = {"Authorization": "Bearer verify"}
EXAMPLES = ["RELIANCE", "HDFCBANK", "ITC", "3IINFOLTD", "TATACOMM", "INFY"]


class _Verifier:
    def verify(self, token: str) -> Claims:
        return Claims("verify-admin", "verify@example.com", email_verified=True)


class _State:
    def __init__(self) -> None:
        self.user = User(uid="verify-admin", enabled=True, role="admin")
        self.lists: dict[str, Watchlist] = {}

    def get_user(self, uid: str) -> User | None:
        return self.user

    def create_user(self, user: User) -> User:
        return self.user

    def update_user(self, uid: str, *, enabled: bool | None, role: Role | None) -> User:
        raise UserNotFound(uid)

    def list_users(self) -> list[User]:
        return [self.user]

    def list_watchlists(self, uid: str) -> list[Watchlist]:
        return list(self.lists.values())

    def put_watchlist(self, uid: str, watchlist: Watchlist) -> Watchlist:
        self.lists[watchlist.watchlist_id] = watchlist
        return watchlist

    def delete_watchlist(self, uid: str, watchlist_id: str) -> bool:
        return self.lists.pop(watchlist_id, None) is not None


def timed(client: TestClient, path: str, **params: Any) -> tuple[Any, float, int]:
    t0 = time.perf_counter()
    r = client.get(path, params=params, headers=H)
    dt = time.perf_counter() - t0
    assert r.status_code == 200, (path, r.status_code, r.text[:300])
    return r.json(), dt, len(r.content)


def main(lake: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    store = LocalObjectStore(lake)
    settings = ChartLensSettings.model_construct(api=ApiConfig(firebase_project_id="verify"))
    tracemalloc.start()
    snapshots = SnapshotProvider(store, "NSE")
    t0 = time.perf_counter()
    snap = snapshots.get()
    load_seconds = time.perf_counter() - t0
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    app = create_app(settings)
    app.dependency_overrides.update(
        {
            settings_dep: lambda: settings,
            verifier_dep: _Verifier,
            appstate_dep: _State,
            snapshots_dep: lambda: snapshots,
        }
    )
    client = TestClient(app)
    report: dict[str, Any] = {
        "snapshot": {
            "meta_version": snap.meta_version,
            "as_of": str(snap.as_of),
            "versions": snap.versions,
            "counts": snap.counts,
            "load_seconds": round(load_seconds, 2),
            "memory_mb_after_load": round(current / 1e6, 1),
            "memory_mb_peak": round(peak / 1e6, 1),
        },
        "timings": {},
        "examples": {},
        "checks": {},
    }
    metas: set[str] = set()

    status, dt, size = timed(client, "/api/v1/system/status")
    metas.add(status["meta_version"])
    report["timings"]["system_status"] = {"ms": round(dt * 1000, 1), "bytes": size}
    for q in ("RELIANCE", "reli", "INE002A01018", "hdfc bank", "3IINFOTECH", "TATAMTRDVR"):
        body, dt, size = timed(client, "/api/v1/securities", q=q, universe="all")
        metas.add(body["meta_version"])
        report["timings"][f"search:{q}"] = {
            "ms": round(dt * 1000, 1),
            "top": [
                (r["symbol"], r["instrument_type"], r["listing_status"])
                for r in body["results"][:3]
            ],
        }

    for symbol in EXAMPLES:
        found, _, _ = timed(client, "/api/v1/securities", q=symbol, universe="all")
        hit = next((r for r in found["results"] if r["symbol"] == symbol), None)
        if hit is None:
            report["examples"][symbol] = "not found"
            continue
        sid = hit["security_id"]
        detail, d1, _ = timed(client, f"/api/v1/securities/{sid}")
        cold, d2, s2 = timed(client, f"/api/v1/securities/{sid}/weekly")
        warm, d3, _ = timed(client, f"/api/v1/securities/{sid}/weekly")
        full, d4, s4 = timed(client, f"/api/v1/securities/{sid}/weekly", segments="all")
        dq, d5, _ = timed(client, f"/api/v1/securities/{sid}/data-quality")
        metas.update(x["meta_version"] for x in (detail, cold, full, dq))
        specials = [
            (b["iso_year"], b["iso_week"], b["last_session_date"], b["closing_session_type"])
            for b in full["bars"]
            if b["closes_on_special_session"]
        ]
        report["examples"][symbol] = {
            "security_id": sid,
            "usable_from": detail["usable_from"],
            "segments": [
                (s["segment_start"], s["segment_end"], s["cause"]) for s in detail["segments"]
            ],
            "bars_valid_segment": len(cold["bars"]),
            "bars_all_segments": len(full["bars"]),
            "first_valid_bar": cold["bars"][0] if cold["bars"] else None,
            "last_bar": cold["bars"][-1] if cold["bars"] else None,
            "closing_on_special_sessions_last_6": specials[-6:],
            "findings": len(dq["findings"]),
            "ms": {
                "detail": round(d1 * 1000, 1),
                "weekly_cold": round(d2 * 1000, 1),
                "weekly_warm": round(d3 * 1000, 1),
                "weekly_all": round(d4 * 1000, 1),
                "data_quality": round(d5 * 1000, 1),
            },
            "bytes": {"weekly_valid": s2, "weekly_all": s4},
        }
        assert warm == cold

    any_sid = next(iter(snap.securities))
    r = client.get(
        f"/api/v1/securities/{any_sid}/weekly", params={"as_of": "2015-06-30"}, headers=H
    )
    report["checks"] = {
        "one_meta_version_across_all_responses": len(metas) == 1,
        "point_in_time_request_refused": r.status_code == 400,
        "unauthenticated_refused": client.get("/api/v1/system/status").status_code == 401,
    }
    (out / "m5-report.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps({k: report[k] for k in ("snapshot", "checks")}, indent=2, default=str))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
