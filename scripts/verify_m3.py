"""Milestone 3 verification report (used by .github/workflows/m3-verify.yml).

Reads a local lake after ``corporate-actions fetch``, ``adjust`` and ``data-quality``
and writes a compact JSON review: the market-wide discontinuity report, programmatic
checks of the seven adjustment requirements, event statistics, worked examples (raw vs
adjusted prices around every event) and review queues. Never copies bulk data out.

usage: python scripts/verify_m3.py LAKE_DIR OUT_DIR [ADJUST_RUN2_JSON]
"""

from __future__ import annotations

import bisect
import json
import random
import sys
from collections import Counter
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from chartlens_core.adjustment import adjust_price, parse_fraction
from chartlens_core.config import get_settings
from chartlens_pipeline.storage import (
    DataLakeLayout,
    LocalObjectStore,
    ObjectStore,
    object_store_from_config,
)

EX = "NSE"
EXAMPLES = [
    "HDFCBANK",
    "TATACOMM",
    "3IINFOLTD",
    "RELIANCE",
    "INFY",
    "TCS",
    "WIPRO",
    "ITC",
    "BAJFINANCE",
    "VEDL",
    "ETERNAL",
]


def table(store: ObjectStore, key: str) -> list[dict[str, Any]]:
    return pq.read_table(pa.BufferReader(store.get(key))).to_pylist()


def window(rows: list[dict[str, Any]], day: date, before: int = 3, after: int = 2) -> list[str]:
    dates = [r["trading_date"] for r in rows]
    i = bisect.bisect_left(dates, day)
    out = []
    for r in rows[max(0, i - before) : i + after]:
        out.append(
            f"{r['trading_date']} {r['symbol']:<12} raw O/C {r['open']}/{r['close']}  "
            f"adj O/C {r['adj_open']}/{r['adj_close']}  factor {r['price_factor']}"
            + ("  BREAK" if r["break_before"] else "")
        )
    return out


def factor_from_components(
    components: list[dict[str, Any]], inputs: dict[str, str]
) -> Fraction | None:
    f = Fraction(1)
    for c in components:
        kind = c["kind"]
        if kind in ("SPLIT", "CONSOLIDATION"):
            f *= Fraction(Decimal(c["fv_to"])) / Fraction(Decimal(c["fv_from"]))
        elif kind == "BONUS":
            a, b = Fraction(Decimal(c["ratio_new"])), Fraction(Decimal(c["ratio_held"]))
            f *= b / (a + b)
        elif kind == "RIGHTS":
            a, b = Fraction(Decimal(c["ratio_new"])), Fraction(Decimal(c["ratio_held"]))
            close = Fraction(Decimal(inputs["cum_rights_close"]))
            issue = Fraction(Decimal(inputs["issue_price"]))
            f *= Fraction(1) if issue >= close else (b * close + a * issue) / (a + b) / close
        else:
            return None
    return f


def main(lake_dir: str, out_dir: str, run2: str | None = None) -> None:
    # "-" = the configured store (e.g. GCS via CHARTLENS_STORAGE__BACKEND=gcs)
    store = (
        object_store_from_config(get_settings().storage)
        if lake_dir == "-"
        else LocalObjectStore(Path(lake_dir))
    )
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    adj_report = json.loads(store.get(DataLakeLayout.adjustment_report_key(EX)))
    dq_report = json.loads(store.get(DataLakeLayout.data_quality_report_key(EX)))
    manifest = json.loads(store.get(DataLakeLayout.adjusted_manifest_key(EX)))
    events = table(store, DataLakeLayout.adjustment_events_key(EX))
    actions = table(store, DataLakeLayout.corporate_actions_table_key(EX))
    statuses = {
        s["security_id"]: s for s in table(store, DataLakeLayout.data_quality_status_key(EX))
    }
    findings = table(store, DataLakeLayout.data_quality_findings_key(EX))
    history = table(store, DataLakeLayout.identifier_history_key(EX))

    def adjusted(sid: str) -> list[dict[str, Any]]:
        return table(store, DataLakeLayout.adjusted_daily_key(EX, sid))

    # ------------------------------------------------------------ requirement checks
    applied = [e for e in events if e["applied"]]
    checks: dict[str, Any] = {}
    checks["1_identifiable_source"] = {
        "applied": len(applied),
        "without_source": [
            f"{e['security_id']}@{e['ex_date']}"
            for e in applied
            if not json.loads(e["record_keys"]) and not e["override"]
        ],
    }
    mismatched = []
    for e in applied:
        if e["override"]:
            continue
        expected = factor_from_components(json.loads(e["components"]), json.loads(e["inputs"]))
        if expected is None or expected != parse_fraction(e["factor"]):
            mismatched.append(f"{e['security_id']}@{e['ex_date']} {e['factor']} vs {expected}")
    checks["2_deterministic_formula"] = {"recomputed": len(applied), "mismatched": mismatched[:50]}
    checks["3_ex_date_gap_reduced"] = {
        "violations": [
            f"{e['security_id']}@{e['ex_date']}"
            for e in applied
            if e["residual_log"] is None
            or (
                abs(e["residual_log"]) >= abs(e["raw_gap_log"])
                and not (
                    e["status"] == "CONSISTENT"
                    and abs(e["residual_log"]) <= min(e["tolerance_log"], 0.2231435513142097)
                )
            )
        ]
    }
    # 4: reproducible — recompute a sample of adjusted rows exactly from raw × factor.
    rng = random.Random(20261001)
    sample_sids = rng.sample(sorted(manifest["files"]), min(150, len(manifest["files"])))
    with_factor = sorted({e["security_id"] for e in applied} & set(manifest["files"]))
    sample_sids += rng.sample(with_factor, min(150, len(with_factor)))
    rows_checked = bad_rows = 0
    for sid in sample_sids:
        for r in adjusted(sid):
            f = parse_fraction(r["price_factor"])
            for raw, adj in (
                ("open", "adj_open"),
                ("high", "adj_high"),
                ("low", "adj_low"),
                ("close", "adj_close"),
            ):
                rows_checked += 1
                if adjust_price(r[raw], f) != r[adj]:
                    bad_rows += 1
    second = json.loads(Path(run2).read_text()) if run2 else None
    checks["4_reproducible"] = {
        "values_recomputed": rows_checked,
        "values_mismatched": bad_rows,
        "second_run_same_version": second is not None
        and second["adjustment_version"] == adj_report["adjustment_version"],
        "second_run_files_written": second["counts"]["files_written"] if second else None,
    }
    checks["5_consistent_or_suspect"] = dict(Counter(e["status"] for e in applied))
    d = adj_report["discontinuity"]
    checks["6_no_new_discontinuity"] = {
        "new_gaps_introduced": d["new_gaps_introduced"],
        "gaps_worsened": d["gaps_worsened"],
        "examples": d["new_gap_examples"],
    }
    checks["7_unquantified_unadjusted"] = {
        "violations": [
            f"{e['security_id']}@{e['ex_date']}"
            for e in applied
            if e["action_class"] == "UNQUANTIFIED" and not e["override"]
        ]
    }
    checks["all_passed"] = (
        not checks["1_identifiable_source"]["without_source"]
        and not mismatched
        and not checks["3_ex_date_gap_reduced"]["violations"]
        and bad_rows == 0
        and (second is None or checks["4_reproducible"]["second_run_same_version"])
        and d["new_gaps_introduced"] == 0
        and d["gaps_worsened"] == 0
        and not checks["7_unquantified_unadjusted"]["violations"]
    )

    discontinuity = {
        "threshold": f"|open / previous close - 1| > {d['threshold']}",
        "before_adjustment_large_gaps_X": d["raw_large_gaps"],
        "explained_and_removed_by_applied_corporate_actions": d["resolved_by_adjustment"],
        "after_adjustment_large_gaps_Y": d["adjusted_large_gaps"],
        "new_gaps_introduced (must be 0)": d["new_gaps_introduced"],
        "gaps_worsened (must be 0)": d["gaps_worsened"],
        "remaining_at_unquantified_events (hard discontinuities)": d["at_unquantified_events"],
        "remaining_at_rejected_or_suspect_events": d["at_rejected_or_suspect_events"],
        "remaining_at_cash_distribution_ex_dates": d["at_cash_distributions"],
        "remaining_at_reviewed_identity_breaks": d.get("at_reviewed_identity_breaks"),
        "remaining_unexplained_Z": d["unexplained"],
    }

    # ------------------------------------------------------------ worked examples
    sids_by_symbol: dict[str, set[str]] = {}
    for h in history:
        if h["identifier_type"] == "SYMBOL":
            sids_by_symbol.setdefault(h["identifier_value"], set()).add(h["security_id"])
    examples: dict[str, Any] = {}
    for symbol in EXAMPLES:
        for sid in sorted(sids_by_symbol.get(symbol, set())):
            if sid not in manifest["files"]:
                continue
            rows = adjusted(sid)
            evs = [e for e in events if e["security_id"] == sid]
            examples[f"{symbol} {sid}"] = {
                "identifiers": [
                    f"{h['identifier_type']} {h['identifier_value']} "
                    f"{h['valid_from']}→{h['valid_to']} ({h['evidence']})"
                    for h in history
                    if h["security_id"] == sid and h["identifier_type"] in ("SYMBOL", "ISIN")
                ],
                "quality": statuses.get(sid),
                "findings": [
                    f"{f['start_date']} {f['severity']} {f['code']}"
                    f"{' BREAK' if f['breaks_continuity'] else ''}: {f['detail']}"
                    for f in findings
                    if f["security_id"] == sid and f["code"] not in ("UNEXPLAINED_MOVE",)
                ][:40],
                "unexplained_moves": sum(
                    1
                    for f in findings
                    if f["security_id"] == sid and f["code"] == "UNEXPLAINED_MOVE"
                ),
                "events": [
                    {
                        "ex_date": e["ex_date"],
                        "status": e["status"],
                        "method": e["method"],
                        "factor": e["factor"],
                        "subjects": json.loads(e["subjects"]),
                        "inputs": json.loads(e["inputs"]),
                        "raw_gap_log": e["raw_gap_log"],
                        "residual_log": e["residual_log"],
                        "tolerance_log": e["tolerance_log"],
                        "breaks_continuity": e["breaks_continuity"],
                        "notes": json.loads(e["notes"]),
                        "prices": window(rows, e["boundary_date"] or e["ex_date"]),
                    }
                    for e in evs
                ],
                "first_and_last_rows": window(rows, rows[0]["trading_date"], 0, 1)
                + window(rows, rows[-1]["trading_date"], 0, 1),
            }

    # ------------------------------------------------------------ review queues
    def ev_line(e: dict[str, Any]) -> str:
        return (
            f"{e['security_id']} ex {e['ex_date']} {e['status']} f={e['factor']} "
            f"gap={e['raw_gap_log']} res={e['residual_log']} "
            f"| {'; '.join(json.loads(e['subjects']))}"
            f" | {'; '.join(json.loads(e['notes']))}"
        )

    symbol_of = {s["security_id"]: s["symbol"] for s in statuses.values()}
    queues = {
        "rejected_by_price": [
            symbol_of.get(e["security_id"], "?") + " " + ev_line(e)
            for e in events
            if e["status"] == "REJECTED_BY_PRICE"
        ][:200],
        "suspect": [
            symbol_of.get(e["security_id"], "?") + " " + ev_line(e)
            for e in events
            if e["status"] == "SUSPECT"
        ][:200],
        "conflicting_records": [
            symbol_of.get(e["security_id"], "?") + " " + ev_line(e)
            for e in events
            if e["status"] == "CONFLICTING_RECORDS"
        ][:100],
        "unquantified_sample": [
            symbol_of.get(e["security_id"], "?") + " " + ev_line(e)
            for e in events
            if e["status"] == "UNQUANTIFIED"
        ][:150],
        "unresolved_price_relevant_records": [
            f"{a['ex_date']} {a['symbol']} {a['series']} {a['isin']} {a['resolution']}: "
            f"{a['subject']} ({a['resolution_detail']})"
            for a in actions
            if a["security_id"] is None
            and a["action_class"] in ("EQUITY_ADJUSTMENT", "UNQUANTIFIED")
            and a["resolution"] in ("UNRESOLVED", "CONFLICT")
        ][:200],
        "unrecognised_records": Counter(
            a["subject"] for a in actions if a["action_class"] == "UNRECOGNISED"
        ).most_common(80),
        "largest_unexplained_moves": sorted(
            (
                f"{symbol_of.get(f['security_id'], '?')} {f['start_date']} {f['detail']}"
                for f in findings
                if f["code"] == "UNEXPLAINED_MOVE"
            ),
            key=lambda s: -abs(float(s.split("adjusted close ")[1].split("%")[0])),
        )[:100],
    }

    as_of = date.fromisoformat(dq_report["as_of"])
    active = [s for s in statuses.values() if (as_of - s["last_date"]).days <= 30]
    usable_years = Counter(s["usable_from"].year for s in active if s["usable_from"] is not None)
    stats = {
        "events_by_status": adj_report["counts"]["events_by_status"],
        "events_by_method": adj_report["counts"]["events_by_method"],
        "records_by_class": adj_report["counts"]["records_by_class"],
        "records_by_resolution": adj_report["counts"]["records_by_resolution"],
        "feed_windows_missing": adj_report["counts"]["feed_windows_missing"],
        "dq_status_counts": dq_report["status_counts"],
        "dq_active_status_counts": dq_report["active_status_counts"],
        "dq_breaks_by_code": dq_report["breaks_by_code"],
        "dq_findings_by_code": dq_report["findings_by_code"],
        "active_usable_from_year_histogram": dict(sorted(usable_years.items())),
    }
    # ------------------------------------------------------------ investigations
    first_dates = {sid: s["first_date"] for sid, s in statuses.items()}
    moved = [s for s in active if s["usable_from"] and s["usable_from"] > s["first_date"]]
    break_codes: dict[str, list[tuple[date, str]]] = {}
    for f in findings:
        if f["breaks_continuity"] and f["security_id"]:
            break_codes.setdefault(f["security_id"], []).append((f["start_date"], f["code"]))
    latest_break = Counter(
        (s["usable_from"].year, max(break_codes.get(s["security_id"], [(date.min, "?")]))[1])
        for s in moved
    )
    gaps_key = DataLakeLayout.unexplained_gaps_key(EX)
    z: dict[str, Any] = {}
    if store.exists(gaps_key):
        gaps = table(store, gaps_key)
        cats: Counter[str] = Counter()
        for g in gaps:
            if g["prev_close"] < 2:
                cats["price below Rs 2 (tick-size moves)"] += 1
            elif g["session_index"] < 5:
                cats["first 5 sessions after listing / series entry"] += 1
            elif g["days_since_previous_session"] > 14:
                cats["after > 14 days without a trade"] += 1
            elif abs(g["gap"]) > 0.6:
                cats["other, |gap| > 60%"] += 1
            else:
                cats["other, 25-60%"] += 1
        big_other = sorted(
            (
                g
                for g in gaps
                if g["prev_close"] >= 2
                and g["session_index"] >= 5
                and g["days_since_previous_session"] <= 14
                and abs(g["gap"]) > 0.6
            ),
            key=lambda g: -abs(g["gap"]),
        )
        z = {
            "total": len(gaps),
            "by_category": dict(cats.most_common()),
            "by_year": dict(sorted(Counter(g["trading_date"].year for g in gaps).items())),
            "largest_other": [
                f"{symbol_of.get(g['security_id'], '?')} {g['trading_date']} {g['gap']:+.1%} "
                f"prev close {g['prev_close']}"
                for g in big_other[:60]
            ],
        }
    re_like = sorted(
        {
            (h["identifier_value"], h["security_id"])
            for h in history
            if h["identifier_type"] == "SYMBOL" and h["identifier_value"].endswith("-RE")
        }
    )
    series_of: dict[str, set[str]] = {}
    for h in history:
        if h["identifier_type"] == "SERIES":
            series_of.setdefault(h["security_id"], set()).add(h["identifier_value"])
    three_i = [
        {
            k: a[k]
            for k in (
                "ex_date",
                "symbol",
                "series",
                "isin",
                "subject",
                "action_class",
                "security_id",
                "resolution",
                "resolution_detail",
            )
        }
        for a in actions
        if (a["isin"] or "").startswith("INE748C") or a["symbol"] in ("3IINFOTECH", "3IINFOLTD")
    ]
    dvr = {
        sid: [
            f"{h['identifier_type']} {h['identifier_value']} {h['valid_from']}→{h['valid_to']}"
            for h in history
            if h["security_id"] == sid
        ]
        for sid in {
            h["security_id"]
            for h in history
            if h["identifier_value"] in ("TATAMTRDVR", "IN9155A01012", "SUMEETINDS", "INE235C01010")
        }
    }

    isins_of: dict[str, set[str]] = {}
    symbols_of: dict[str, set[str]] = {}
    for h in history:
        if h["identifier_type"] == "ISIN":
            isins_of.setdefault(h["security_id"], set()).add(h["identifier_value"])
        elif h["identifier_type"] == "SYMBOL":
            symbols_of.setdefault(h["security_id"], set()).add(h["identifier_value"])

    def kind(sid: str) -> str:
        isins, symbols = isins_of.get(sid, set()), symbols_of.get(sid, set())
        if any(i.startswith("INF") for i in isins):
            return "ETF/MF unit (INF ISIN)"
        if any(sym.endswith("-RE") for sym in symbols):
            return "rights entitlement (-RE)"
        if any(len(i) == 12 and i[7:9] != "01" for i in isins):
            return f"non-01 security type ({sorted(i[7:9] for i in isins)[0]})"
        if not isins:
            return "no ISIN (pre-2011 only)"
        return "equity share (01)"

    kinds_all = Counter(kind(sid) for sid in statuses)
    kinds_active = Counter(kind(s["security_id"]) for s in active)
    kinds_2026 = Counter(
        kind(s["security_id"]) for s in active if first_dates[s["security_id"]].year == 2026
    )
    re_isins = sorted(
        {
            h["identifier_value"]
            for h in history
            if h["identifier_type"] == "ISIN"
            and any(
                x["identifier_value"].endswith("-RE")
                for x in history
                if x["security_id"] == h["security_id"] and x["identifier_type"] == "SYMBOL"
            )
        }
    )[:15]
    z_by_kind: Counter[str] = Counter()
    if store.exists(gaps_key):
        for g in table(store, gaps_key):
            z_by_kind[kind(g["security_id"])] += 1
    investigations_extra = {
        "instrument_kinds_all": dict(kinds_all.most_common()),
        "instrument_kinds_active": dict(kinds_active.most_common()),
        "instrument_kinds_active_first_listed_2026": dict(kinds_2026.most_common()),
        "re_isin_samples": re_isins,
        "z_by_instrument_kind": dict(z_by_kind.most_common()),
    }
    investigations = {
        **investigations_extra,
        "active_moved_usable_from_by_year_and_latest_break": {
            f"{y} {c}": n for (y, c), n in sorted(latest_break.items())
        },
        "active_new_listings_by_first_year": dict(
            sorted(Counter(first_dates[s["security_id"]].year for s in active).items())
        ),
        "unexplained_gaps_Z": z,
        "re_symbols": [
            f"{sym} {sid} series={sorted(series_of.get(sid, set()))}" for sym, sid in re_like
        ][:40],
        "re_symbol_count": len(re_like),
        "three_i_feed_records": three_i,
        "identity_conflict_cases": dvr,
    }
    (out / "m3-investigations.json").write_text(json.dumps(investigations, indent=2, default=str))

    summary = {
        "adjustment_version": adj_report["adjustment_version"],
        "identity_version": adj_report["identity_version"],
        "dq_version": dq_report["dq_version"],
        "data_end": adj_report["data_end"],
        "published": adj_report["published"],
        "securities": adj_report["counts"]["securities"],
        "rows": adj_report["counts"]["rows"],
        "rows_with_factor": adj_report["counts"]["rows_with_factor"],
        "securities_with_applied_factors": adj_report["counts"]["securities_with_applied_factors"],
        "requirements": checks,
        "discontinuity_report": discontinuity,
        "stats": stats,
    }
    (out / "m3-summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (out / "m3-examples.json").write_text(json.dumps(examples, indent=2, default=str))
    (out / "m3-queues.json").write_text(json.dumps(queues, indent=2, default=str))
    print(json.dumps({"requirements_all_passed": checks["all_passed"], **discontinuity}, indent=2))


if __name__ == "__main__":
    main(*sys.argv[1:])
