"""Summarise a real backfill for review (used by .github/workflows/ingest-verify.yml).

Reads a local lake produced by ``chartlens-pipeline backfill`` and writes a compact
JSON report: dataset totals, identity statistics, quarantine breakdown with samples,
the ISIN boundary, security lookups, and one fully traced example day
(source bytes → hash → parser → identity → canonical row → Parquet). Never copies
bulk market data out of the lake.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from chartlens_pipeline.identity import SecurityMaster
from chartlens_pipeline.sources import RawSourceStore
from chartlens_pipeline.storage import DataLakeLayout, LocalObjectStore

EX = "NSE"
LOOKUP_SYMBOLS = [
    "RELIANCE",
    "TCS",
    "INFY",
    "3IINFOLTD",
    "3IINFOTECH",
    "HDFCBANK",
    "ZOMATO",
    "ETERNAL",
]


def read_table(store: LocalObjectStore, key: str) -> pa.Table:
    return pq.read_table(pa.BufferReader(store.get(key)))


def main(lake_dir: str, out_dir: str) -> None:
    store = LocalObjectStore(Path(lake_dir))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifests = {
        date.fromisoformat(json.loads(store.get(k))["trading_date"]): json.loads(store.get(k))
        for k in store.list(DataLakeLayout.ingestion_manifest_prefix(EX))
    }
    status = Counter(m["status"] for m in manifests.values())
    ingested = sorted(d for d, m in manifests.items() if m["status"] == "INGESTED")
    with_isin = [d for d in ingested if manifests[d].get("has_isin")]
    without_isin = [d for d in ingested if not manifests[d].get("has_isin")]
    formats = Counter(manifests[d].get("source_format") for d in ingested)
    format_ranges = {
        f: [
            str(min(d for d in ingested if manifests[d].get("source_format") == f)),
            str(max(d for d in ingested if manifests[d].get("source_format") == f)),
        ]
        for f in formats
    }
    q_reasons: Counter[str] = Counter()
    for d in ingested:
        q_reasons.update(manifests[d].get("quarantine_by_reason", {}))
    links = [
        {"date": str(d), **link}
        for d in ingested
        for link in manifests[d].get("identity_links", [])
    ]
    conflicts = [str(d) for d in ingested if manifests[d].get("source_conflict")]

    securities = read_table(store, DataLakeLayout.securities_key(EX))
    history = read_table(store, DataLakeLayout.identifier_history_key(EX))
    master = SecurityMaster.from_tables(EX, securities, history)
    sec_rows = securities.to_pylist()

    # Quarantine samples per reason (a few rows each, never bulk).
    samples: dict[str, list[dict[str, object]]] = {}
    for key in store.list(DataLakeLayout.quarantine_daily_prefix(EX)):
        for row in read_table(store, key).to_pylist():
            bucket = samples.setdefault(row["reason"], [])
            if len(bucket) < 8:
                bucket.append(
                    {k: str(row[k]) for k in ("trading_date", "symbol", "series", "isin", "detail")}
                )

    lookups: dict[str, object] = {}
    for sym in LOOKUP_SYMBOLS:
        ids = {
            s.security_id
            for s in master.spans()
            if s.identifier_type == "SYMBOL" and s.value == sym
        }
        lookups[sym] = [
            {
                **{
                    k: str(v)
                    for k, v in next(r for r in sec_rows if r["security_id"] == sid).items()
                },
                "identifier_history": [
                    [
                        str(s.identifier_type),
                        s.value,
                        str(s.valid_from),
                        str(s.valid_to),
                        str(s.evidence),
                    ]
                    for s in master.spans()
                    if s.security_id == sid
                ],
            }
            for sid in sorted(ids)
        ]

    # One traced day: the latest ingested session, RELIANCE.
    trace: dict[str, object] = {}
    for day in sorted(ingested, reverse=True)[:1]:
        m = manifests[day]
        raw_store = RawSourceStore(store)
        record = raw_store.get_by_hash(EX, m["selected_source"]["content_hash"])
        assert record is not None
        content = store.get(record.storage_key)
        table = read_table(store, DataLakeLayout.curated_daily_key(EX, day))
        rel = [r for r in table.to_pylist() if r["symbol"] == "RELIANCE"]
        trace = {
            "trading_date": str(day),
            "source_url": record.url,
            "raw_object_key": record.storage_key,
            "raw_bytes": record.byte_size,
            "recorded_sha256": record.content_hash,
            "recomputed_sha256": hashlib.sha256(content).hexdigest(),
            "source_metadata": json.loads(store.get(record.storage_key + ".meta.json")),
            "manifest": {
                k: m[k]
                for k in (
                    "source_format",
                    "parser_version",
                    "counts",
                    "anchored",
                    "calendar_version",
                    "identity_fingerprint",
                )
            },
            "canonical_key": DataLakeLayout.curated_daily_key(EX, day),
            "canonical_schema": str(table.schema),
            "canonical_row": {k: str(v) for k, v in rel[0].items()} if rel else None,
        }

    report = {
        "manifest_status": dict(status),
        "ingested_range": [str(ingested[0]), str(ingested[-1])] if ingested else None,
        "sessions_ingested": len(ingested),
        "sessions_not_published": sorted(
            str(d) for d, m in manifests.items() if m["status"] == "NOT_PUBLISHED"
        ),
        "sessions_failed": {
            str(d): m.get("error") for d, m in manifests.items() if m["status"] == "FAILED"
        },
        "formats": dict(formats),
        "format_ranges": format_ranges,
        "isin_boundary": {
            "last_session_without_isin": str(max(without_isin)) if without_isin else None,
            "first_session_with_isin": str(min(with_isin)) if with_isin else None,
        },
        "rows": {
            "read": sum(manifests[d]["counts"]["rows_read"] for d in ingested),
            "accepted": sum(manifests[d]["counts"]["rows_accepted"] for d in ingested),
            "quarantined": sum(manifests[d]["counts"]["rows_quarantined"] for d in ingested),
            "out_of_scope": sum(manifests[d]["counts"]["rows_out_of_scope"] for d in ingested),
        },
        "quarantine_by_reason": dict(q_reasons.most_common()),
        "quarantine_samples": samples,
        "pending_identity_dates": [
            str(d) for d in ingested if manifests[d].get("pending_identity_rows")
        ],
        "securities": {
            "total": len(sec_rows),
            "by_identity_basis": dict(Counter(r["identity_basis"] for r in sec_rows)),
            "by_listing_status": dict(Counter(r["listing_status"] for r in sec_rows)),
            "with_multiple_isins": sum(
                1 for r in sec_rows if len(master.spans_for(r["security_id"], "ISIN")) > 1
            ),  # type: ignore[arg-type]
            "with_multiple_symbols": sum(
                1
                for r in sec_rows
                if len({s.value for s in master.spans_for(r["security_id"], "SYMBOL")}) > 1  # type: ignore[arg-type]
            ),
        },
        "identifier_spans_by_evidence": dict(Counter(r["evidence"] for r in history.to_pylist())),
        "identity_links": {"count": len(links), "sample": links[:25]},
        "source_conflict_dates": conflicts,
        "lookups": lookups,
        "traced_day": trace,
        "lake_bytes": sum(p.stat().st_size for p in Path(lake_dir).rglob("*") if p.is_file()),
    }
    (out / "verify.json").write_text(json.dumps(report, indent=2, default=str))
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k not in ("lookups", "traced_day", "quarantine_samples")
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
