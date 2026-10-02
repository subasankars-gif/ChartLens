"""Ingestion / backfill end to end: fake NSE archive → real provider → lake."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from nse_fakes import FakeNse, calendar_for, fake_provider, legacy_zip, udiff_zip

from chartlens_core.config import ChartLensSettings
from chartlens_pipeline.daily import (
    DAILY_BAR_SCHEMA_VERSION,
    PRICE_TYPE,
    daily_bar_schema,
    from_parquet_bytes,
)
from chartlens_pipeline.identity import IdentifierType, IdentityOverrides
from chartlens_pipeline.ingest import DateAction, DateStatus, IngestionService
from chartlens_pipeline.providers.nse.bhavcopy import PARSER_VERSION
from chartlens_pipeline.sources import RawSourceStore
from chartlens_pipeline.storage import DataLakeLayout, LocalObjectStore

HEADER = "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,"
MON, TUE, WED, THU, FRI = (date(2024, 1, d) for d in (22, 23, 24, 25, 26))  # FRI = Republic Day
CAL = calendar_for([2024], holidays=[FRI])
TODAY = date(2024, 3, 1)


def bhav(day: date, rows: list[tuple[str, str, str | None, str]]) -> bytes:
    """rows: (symbol, series, isin, close). OHLC built around close so rows are valid."""
    stamp = day.strftime("%d-%b-%Y").upper()
    lines = [HEADER]
    for sym, series, isin, close in rows:
        c = Decimal(close)
        lines.append(
            f"{sym},{series},{c},{c + 1},{c - 1},{c},{c},{c},1000,{c * 1000},{stamp},10,{isin or ''},"
        )
    return "\n".join(lines).encode()


BASE_ROWS = [("RELIANCE", "EQ", "INE002A01018", "2500"), ("TCS", "EQ", "INE467B01029", "3700")]


@pytest.fixture
def fake() -> FakeNse:
    return FakeNse()


@pytest.fixture
def lake(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "lake")


def service(fake: FakeNse, lake: LocalObjectStore, calendar=CAL) -> IngestionService:  # type: ignore[no-untyped-def]
    settings = ChartLensSettings.model_construct()
    return IngestionService(
        settings,
        fake_provider(fake, calendar, settings),
        lake,
        overrides=IdentityOverrides(),  # isolated from the repository's reviewed overrides
        today=lambda: TODAY,
    )


def serve_week(fake: FakeNse, rows=BASE_ROWS) -> None:  # type: ignore[no-untyped-def]
    for day in (MON, TUE, WED, THU):
        fake.serve(*legacy_zip(day, bhav(day, rows)))


def canonical(lake: LocalObjectStore, day: date) -> pa.Table:
    return from_parquet_bytes(
        lake.get(DataLakeLayout.curated_daily_key("NSE", day)), daily_bar_schema()
    )


def manifest(lake: LocalObjectStore, day: date) -> dict[str, object]:
    return json.loads(lake.get(DataLakeLayout.ingestion_manifest_key("NSE", day)))


# ----------------------------------------------------------------------------- single day


def test_single_day_end_to_end(fake: FakeNse, lake: LocalObjectStore) -> None:
    fake.serve(*legacy_zip(MON, bhav(MON, BASE_ROWS)))
    report = service(fake, lake).backfill(MON, MON)
    m = report.metrics
    assert (m.dates_ingested, m.files_downloaded, m.rows_accepted, m.securities_created) == (
        1,
        1,
        2,
        2,
    )
    assert m.errors == []

    table = canonical(lake, MON)
    assert table.num_rows == 2
    assert table.schema.field("close").type == PRICE_TYPE
    assert (
        table.schema.metadata[b"chartlens.schema_version"] == str(DAILY_BAR_SCHEMA_VERSION).encode()
    )
    rel = next(r for r in table.to_pylist() if r["symbol"] == "RELIANCE")
    assert rel["close"] == Decimal("2500") and rel["parser_version"] == PARSER_VERSION
    assert rel["trading_date"] == MON and rel["source_file_date"] == MON

    man = manifest(lake, MON)
    assert man["status"] == DateStatus.INGESTED and man["source_format"] == "legacy"
    assert man["counts"]["rows_accepted"] == 2  # type: ignore[index]


def test_canonical_rows_trace_back_to_exact_source_bytes(
    fake: FakeNse, lake: LocalObjectStore
) -> None:
    url, body = legacy_zip(MON, bhav(MON, BASE_ROWS))
    fake.serve(url, body)
    service(fake, lake).backfill(MON, MON)
    row = canonical(lake, MON).to_pylist()[0]
    record = RawSourceStore(lake).get_by_hash("NSE", row["source_file_hash"])
    assert record is not None and record.url == url
    raw = RawSourceStore(lake).load(record)
    assert raw == body and hashlib.sha256(raw).hexdigest() == row["source_file_hash"]


# ------------------------------------------------------------------- ranges, holidays, missing days


def test_range_processes_sessions_only_and_flags_missing_ones(
    fake: FakeNse, lake: LocalObjectStore
) -> None:
    for day in (MON, TUE, THU):  # WED is an expected session but not published
        fake.serve(*legacy_zip(day, bhav(day, BASE_ROWS)))
    report = service(fake, lake).backfill(MON, date(2024, 1, 28))
    m = report.metrics
    assert m.requested_days == 7 and m.trading_sessions == 4  # FRI holiday, weekend skipped
    assert (m.dates_ingested, m.files_not_published) == (3, 1)
    assert report.missing_sessions == ["2024-01-24"]
    wed = manifest(lake, WED)
    assert (
        wed["status"] == DateStatus.NOT_PUBLISHED
        and wed["dq_condition"] == "EXPECTED_SESSION_NOT_PUBLISHED"
    )
    assert not lake.exists(
        DataLakeLayout.curated_daily_key("NSE", WED)
    )  # no zero-volume bars invented
    assert not any(
        "2024-01-26" in r or "2024-01-27" in r for r in fake.requests
    )  # holiday/weekend never fetched


def test_dates_are_processed_newest_first(fake: FakeNse, lake: LocalObjectStore) -> None:
    serve_week(fake)
    service(fake, lake).backfill(MON, THU)
    fetched = [r for r in fake.requests if "bhav.csv.zip" in r]
    assert [r.split("/cm")[1][:9] for r in fetched] == [
        "25JAN2024",
        "24JAN2024",
        "23JAN2024",
        "22JAN2024",
    ]


def test_dry_run_makes_no_requests_and_writes_nothing(
    fake: FakeNse, lake: LocalObjectStore
) -> None:
    serve_week(fake)
    report = service(fake, lake).backfill(MON, THU, dry_run=True)
    assert fake.requests == [] and lake.list("") == []
    assert report.plan == {d.isoformat(): str(DateAction.DOWNLOAD) for d in (THU, WED, TUE, MON)}
    assert "Backfill plan (dry run)" in report.render()


def test_partial_failure_does_not_stop_the_run(fake: FakeNse, lake: LocalObjectStore) -> None:
    serve_week(fake)
    bad_url = legacy_zip(TUE, b"")[0]
    fake.fail(bad_url, 503)
    report = service(fake, lake).backfill(MON, THU)
    assert (report.metrics.dates_ingested, report.metrics.dates_failed) == (3, 1)
    assert manifest(lake, TUE)["status"] == DateStatus.FAILED
    assert "503" in report.metrics.errors[0] or "SERVER_ERROR" in report.metrics.errors[0]

    fake.errors.clear()  # NSE recovers; the next run picks up only the failed date
    fake.requests.clear()
    again = service(fake, lake).backfill(MON, THU)
    assert (again.metrics.dates_ingested, again.metrics.dates_skipped) == (1, 3)


def test_forbidden_response_is_a_failure_not_not_published(
    fake: FakeNse, lake: LocalObjectStore
) -> None:
    url, _ = legacy_zip(MON, b"")
    fake.fail(url, 403)
    report = service(fake, lake).backfill(MON, MON)
    assert report.metrics.files_failed == 1 and report.metrics.files_not_published == 0
    assert manifest(lake, MON)["status"] == DateStatus.FAILED


def test_not_published_dates_are_rechecked_only_while_recent(
    fake: FakeNse, lake: LocalObjectStore
) -> None:
    svc = service(fake, lake)
    svc.backfill(MON, MON)
    assert (
        svc.plan_date(MON, refetch=False, reprocess=False) is DateAction.SKIP
    )  # old: known missing
    recent = IngestionService(
        svc.settings, svc.provider, lake, overrides=svc.overrides, today=lambda: date(2024, 1, 25)
    )
    assert recent.plan_date(MON, refetch=False, reprocess=False) is DateAction.DOWNLOAD


# --------------------------------------------------------------------------- idempotency & versions


def test_rerun_is_idempotent(fake: FakeNse, lake: LocalObjectStore) -> None:
    serve_week(fake)
    service(fake, lake).backfill(MON, THU)
    before = {k: lake.get(k) for k in lake.list("") if not k.startswith("metadata/security_master")}
    fake.requests.clear()
    report = service(fake, lake).backfill(MON, THU)
    assert report.metrics.dates_skipped == 4 and report.metrics.dates_ingested == 0
    assert not any("bhav.csv.zip" in r for r in fake.requests)  # nothing re-downloaded
    after = {k: lake.get(k) for k in lake.list("") if not k.startswith("metadata/security_master")}
    assert after == before


def test_refetch_with_identical_content_adds_no_version(
    fake: FakeNse, lake: LocalObjectStore
) -> None:
    fake.serve(*legacy_zip(MON, bhav(MON, BASE_ROWS)))
    service(fake, lake).backfill(MON, MON)
    report = service(fake, lake).backfill(MON, MON, refetch=True)
    assert report.metrics.files_already_stored == 1 and report.metrics.source_conflicts == 0
    assert len(RawSourceStore(lake).list_for_date("NSE", "bhavcopy", MON)) == 1


def test_reissued_file_keeps_both_versions_and_flags_the_conflict(
    fake: FakeNse, lake: LocalObjectStore
) -> None:
    url, original = legacy_zip(MON, bhav(MON, BASE_ROWS))
    fake.serve(url, original)
    service(fake, lake).backfill(MON, MON)

    corrected = [("RELIANCE", "EQ", "INE002A01018", "2510"), BASE_ROWS[1]]
    fake.serve(*legacy_zip(MON, bhav(MON, corrected)))
    report = service(fake, lake).backfill(MON, MON, refetch=True)

    versions = RawSourceStore(lake).list_for_date("NSE", "bhavcopy", MON)
    assert len(versions) == 2 and RawSourceStore(lake).load(versions[0]) == original
    assert report.metrics.source_conflicts == 1
    conflict = json.loads(lake.get(DataLakeLayout.conflict_key("NSE", MON)))
    assert conflict["selection_rule"] == "most_recently_downloaded"
    diff = conflict["other_versions"][0]["rows_with_different_values"]
    assert [d["key"] for d in diff] == [["RELIANCE", "EQ"]]
    rel = next(r for r in canonical(lake, MON).to_pylist() if r["symbol"] == "RELIANCE")
    assert rel["close"] == Decimal("2510") and rel["source_file_hash"] == versions[1].content_hash
    man = manifest(lake, MON)
    assert man["source_versions"] == [v.content_hash for v in versions]


def test_parser_version_change_reprocesses_from_stored_bytes(
    fake: FakeNse, lake: LocalObjectStore
) -> None:
    fake.serve(*legacy_zip(MON, bhav(MON, BASE_ROWS)))
    service(fake, lake).backfill(MON, MON)
    fake.requests.clear()
    svc = service(fake, lake)
    svc.provider.daily_bars.parser_version = "nse_bhavcopy_test_bump"  # type: ignore[misc]
    assert svc.plan_date(MON, refetch=False, reprocess=False) is DateAction.PROCESS_STORED
    svc.backfill(MON, MON)
    assert not any("bhav.csv.zip" in r for r in fake.requests)


# ---------------------------------------------------------------------------- quarantine & identity


def test_invalid_rows_go_to_quarantine_with_context(fake: FakeNse, lake: LocalObjectStore) -> None:
    content = (
        bhav(MON, BASE_ROWS) + b"\nBADROW,EQ,10,5,9,11,11,10,100,1000,22-JAN-2024,1,INE144J01027,"
    )
    fake.serve(*legacy_zip(MON, content))
    report = service(fake, lake).backfill(MON, MON)
    assert report.metrics.quarantine_by_reason == {"INVALID_OHLC": 1}
    q = pq.read_table(
        pa.BufferReader(lake.get(DataLakeLayout.quarantine_daily_key("NSE", MON)))
    ).to_pylist()
    assert q[0]["symbol"] == "BADROW" and q[0]["raw"].startswith("BADROW,EQ,10,5")
    assert q[0]["source_file_hash"] and q[0]["parser_version"] == PARSER_VERSION
    assert "BADROW" not in canonical(lake, MON).column("symbol").to_pylist()


def test_symbol_change_and_series_move_across_days(fake: FakeNse, lake: LocalObjectStore) -> None:
    fake.serve(*legacy_zip(MON, bhav(MON, [("OLDCO", "EQ", "INE467B01029", "10")])))
    fake.serve(*legacy_zip(TUE, bhav(TUE, [("NEWCO", "EQ", "INE467B01029", "10")])))
    fake.serve(*legacy_zip(WED, bhav(WED, [("NEWCO", "BE", "INE467B01029", "10")])))
    svc = service(fake, lake)
    svc.backfill(MON, WED)
    ids = {canonical(lake, d).column("security_id")[0].as_py() for d in (MON, TUE, WED)}
    assert len(ids) == 1 and len(svc.master.securities) == 1
    sid = ids.pop()
    assert svc.master.current(sid, IdentifierType.SYMBOL) == "NEWCO"
    assert svc.master.current(sid, IdentifierType.SERIES) == "BE"


def test_pre_isin_days_resolve_once_anchored(fake: FakeNse, lake: LocalObjectStore) -> None:
    """MON/TUE carry no ISIN (like NSE before 2011); WED onwards does."""
    no_isin = [("RELIANCE", "EQ", None, "2500"), ("TCS", "EQ", None, "3700")]
    fake.serve(*legacy_zip(MON, bhav(MON, no_isin)))
    fake.serve(*legacy_zip(TUE, bhav(TUE, no_isin)))
    fake.serve(*legacy_zip(WED, bhav(WED, BASE_ROWS)))

    # Out of order: the pre-ISIN days alone cannot be resolved yet.
    first = service(fake, lake).backfill(MON, TUE)
    assert first.metrics.unresolved_identifiers == 4 and first.metrics.rows_accepted == 0

    # Ingest the ISIN era, then retry the pending days.
    svc = service(fake, lake)
    svc.backfill(WED, WED)
    retry = svc.reprocess_pending()
    assert retry.metrics.rows_accepted == 4 and retry.metrics.unresolved_identifiers == 0
    wed_ids = set(canonical(lake, WED).column("security_id").to_pylist())
    assert set(canonical(lake, MON).column("security_id").to_pylist()) == wed_ids
    assert len(svc.master.securities) == 2  # no duplicate companies created


def test_descending_backfill_resolves_pre_isin_days_in_one_run(
    fake: FakeNse, lake: LocalObjectStore
) -> None:
    no_isin = [("RELIANCE", "EQ", None, "2500"), ("TCS", "EQ", None, "3700")]
    fake.serve(*legacy_zip(MON, bhav(MON, no_isin)))
    fake.serve(*legacy_zip(TUE, bhav(TUE, BASE_ROWS)))
    report = service(fake, lake).backfill(MON, TUE)
    assert report.metrics.unresolved_identifiers == 0 and report.metrics.rows_accepted == 4


def test_canonical_dataset_has_one_row_per_security_and_date(
    fake: FakeNse, lake: LocalObjectStore
) -> None:
    rows = [
        *BASE_ROWS,
        ("RELIANCE", "BE", "INE002A01018", "2500"),
    ]  # same security twice on one day
    for day in (MON, TUE):
        fake.serve(*legacy_zip(day, bhav(day, rows)))
    report = service(fake, lake).backfill(MON, TUE)
    assert report.metrics.quarantine_by_reason == {"DUPLICATE_SECURITY_DATE": 4}
    keys = [
        (r["security_id"], r["trading_date"])
        for d in (MON, TUE)
        for r in canonical(lake, d).to_pylist()
    ]
    assert len(keys) == len(set(keys))


def test_udiff_names_populate_the_security_master(fake: FakeNse, lake: LocalObjectStore) -> None:
    header = (
        "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,"
        "StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,"
        "SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,"
        "Rsvd1,Rsvd2,Rsvd3,Rsvd4"
    )
    line = (
        "2024-01-22,2024-01-22,CM,NSE,STK,2885,INE002A01018,RELIANCE,EQ,,,,,RELIANCE INDUSTRIES LTD,"
        "2500,2510,2490,2505,2505,2500,,2505,,,1000,2505000,10,F1,1,,,,,"
    )
    fake.serve(*udiff_zip(MON, f"{header}\n{line}".encode()))
    svc = service(fake, lake)
    svc.backfill(MON, MON)
    securities, _ = svc.master.to_tables()
    assert securities.column("security_name").to_pylist() == ["RELIANCE INDUSTRIES LTD"]
    assert manifest(lake, MON)["source_format"] == "udiff"


def test_listing_status_after_backfill(fake: FakeNse, lake: LocalObjectStore) -> None:
    fake.serve(*legacy_zip(MON, bhav(MON, BASE_ROWS)))
    svc = service(fake, lake)
    svc.backfill(MON, MON)
    securities, _ = svc.master.to_tables()
    assert set(securities.column("listing_status").to_pylist()) == {"ACTIVE"}


# -------------------------------------------------------------------- symbol-change evidence & logs


def test_symbol_change_list_is_stored_as_an_immutable_snapshot(
    fake: FakeNse, lake: LocalObjectStore
) -> None:
    fake.symbol_changes = b" Co Ltd,OLDSYM,NEWSYM,15-JAN-2024\n"
    fake.serve(*legacy_zip(MON, bhav(MON, BASE_ROWS)))
    service(fake, lake).backfill(MON, MON)
    snapshot = RawSourceStore(lake).latest_snapshot("NSE", "symbolchange")
    assert snapshot is not None and snapshot.source_type == "symbol_changes"
    assert manifest(lake, MON)["symbol_changes_source"] == snapshot.source_id


def test_structured_log_per_date(
    fake: FakeNse, lake: LocalObjectStore, caplog: pytest.LogCaptureFixture
) -> None:
    fake.serve(*legacy_zip(MON, bhav(MON, BASE_ROWS)))
    with caplog.at_level(logging.INFO, logger="chartlens"):
        report = service(fake, lake).backfill(MON, MON)
    record = next(r for r in caplog.records if r.getMessage() == "ingest.date")
    fields = record.chartlens_fields  # type: ignore[attr-defined]
    assert fields["job_id"] == report.job_id and fields["trading_date"] == "2024-01-22"
    assert (
        fields["rows_read"] == 2 and fields["rows_accepted"] == 2 and fields["rows_rejected"] == 0
    )
    assert len(fields["source_hash"]) == 64 and fields["parser_version"] == PARSER_VERSION
    assert "duration" in fields


def test_check_published_never_trusts_a_bare_200(fake: FakeNse, lake: LocalObjectStore) -> None:
    """Regression: an archive answering 200 with an HTML page is not a published file."""
    url, _ = legacy_zip(MON, b"")
    fake.serve(url, b"<!DOCTYPE html><html>not really here</html>")
    result = service(fake, lake).provider.daily_bars.check_published(MON)
    assert result.status.value == "FAILED"
    assert not any(r.startswith("HEAD") for r in fake.requests)


def test_updated_securities_counts_distinct_securities(
    fake: FakeNse, lake: LocalObjectStore
) -> None:
    """Regression: the 20-year run reported 8.3M 'updated securities' (per-day touches)."""
    serve_week(fake)
    report = service(fake, lake).backfill(MON, THU)
    assert report.metrics.securities_created == 2
    assert report.metrics.securities_updated == 0  # created this run, so not "updated"
    fake.serve(*legacy_zip(date(2024, 1, 29), bhav(date(2024, 1, 29), BASE_ROWS)))
    again = service(fake, lake).backfill(date(2024, 1, 29), date(2024, 1, 29))
    assert again.metrics.securities_updated == 2


# ----------------------------------------------------------------------------- H1: identity inputs


def test_changed_overrides_on_an_existing_master_are_refused(
    fake: FakeNse, lake: LocalObjectStore
) -> None:
    """H1 regression: changing reviewed overrides must not silently mix identity states."""
    from chartlens_pipeline.identity import IdentityOverrides
    from chartlens_pipeline.ingest import IdentityInputsChanged

    serve_week(fake)
    service(fake, lake).backfill(MON, TUE)
    before = {k: lake.get(k) for k in lake.list("")}
    s = ChartLensSettings.model_construct()
    ov = IdentityOverrides(link_isin={"INE467B01029": "INE002A01018"}, fingerprint="reviewed-1")
    changed = IngestionService(
        s, fake_provider(fake, CAL, s), lake, overrides=ov, today=lambda: TODAY
    )
    with pytest.raises(IdentityInputsChanged, match="overrides: none → reviewed-1"):
        changed.backfill(WED, THU)
    with pytest.raises(IdentityInputsChanged):
        changed.backfill(WED, THU, dry_run=True)
    assert {k: lake.get(k) for k in lake.list("")} == before  # nothing written


def test_master_without_recorded_inputs_is_refused(fake: FakeNse, lake: LocalObjectStore) -> None:
    from chartlens_pipeline.ingest import IdentityInputsChanged

    serve_week(fake)
    service(fake, lake).backfill(MON, MON)
    (lake.root / DataLakeLayout.identity_state_key("NSE")).unlink()
    with pytest.raises(IdentityInputsChanged, match="no recorded identity inputs"):
        service(fake, lake).backfill(TUE, TUE)


def test_symbol_change_snapshot_is_pinned_until_rebuild(
    fake: FakeNse, lake: LocalObjectStore
) -> None:
    serve_week(fake)
    fake.symbol_changes = b" Co Ltd,OLDSYM,NEWSYM,15-JAN-2024\n"
    service(fake, lake).backfill(MON, TUE)
    pinned = json.loads(lake.get(DataLakeLayout.identity_state_key("NSE")))["inputs"][
        "symbol_changes"
    ]

    fake.symbol_changes = b" Co Ltd,OLDSYM,NEWSYM,15-JAN-2024\n Other,AAA,BBB,20-JAN-2024\n"
    report = service(fake, lake).backfill(MON, WED)
    assert report.metrics.dates_skipped == 2 and report.metrics.dates_ingested == 1  # no reprocess
    assert any("newer symbol-change snapshot" in w for w in report.metrics.warnings)
    state = json.loads(lake.get(DataLakeLayout.identity_state_key("NSE")))
    assert state["inputs"]["symbol_changes"] == pinned  # still pinned
    assert (
        len([k for k in lake.list("raw/nse/symbolchange/") if k.endswith(".meta.json")]) == 2
    )  # both kept


def test_identity_inputs_ignore_non_identity_methodology(
    fake: FakeNse, lake: LocalObjectStore
) -> None:
    from chartlens_core.config import AdjustmentConfig

    serve_week(fake)
    service(fake, lake).backfill(MON, TUE)
    s = ChartLensSettings.model_construct(adjustment=AdjustmentConfig(dividends=True))
    svc = IngestionService(
        s, fake_provider(fake, CAL, s), lake, overrides=IdentityOverrides(), today=lambda: TODAY
    )
    report = svc.backfill(MON, TUE)  # no IdentityInputsChanged, nothing reprocessed
    assert report.metrics.dates_skipped == 2


def test_identity_inputs_ignore_the_analytical_universe(
    fake: FakeNse, lake: LocalObjectStore
) -> None:
    """Regression (first `daily` on main, 2026-10-02): adding analytical_instrument_types
    changed the identity fingerprint and refused ingestion, though it never affects which
    security a row belongs to."""
    from chartlens_core.config import UniverseConfig

    serve_week(fake)
    service(fake, lake).backfill(MON, TUE)
    s = ChartLensSettings.model_construct(
        universe=UniverseConfig(analytical_instrument_types=("EQUITY_SHARE", "FUND_UNIT"))
    )
    svc = IngestionService(
        s, fake_provider(fake, CAL, s), lake, overrides=IdentityOverrides(), today=lambda: TODAY
    )
    report = svc.backfill(MON, WED)  # no IdentityInputsChanged
    assert report.metrics.dates_skipped == 2 and report.metrics.dates_ingested == 1


def test_identity_relevant_universe_fields_still_count(
    fake: FakeNse, lake: LocalObjectStore
) -> None:
    from chartlens_core.config import UniverseConfig
    from chartlens_pipeline.ingest import IdentityInputsChanged

    serve_week(fake)
    service(fake, lake).backfill(MON, TUE)
    s = ChartLensSettings.model_construct(universe=UniverseConfig(series=("EQ",)))
    svc = IngestionService(
        s, fake_provider(fake, CAL, s), lake, overrides=IdentityOverrides(), today=lambda: TODAY
    )
    with pytest.raises(IdentityInputsChanged, match="config:"):
        svc.backfill(WED, WED)


def test_every_universe_field_is_classified_for_identity() -> None:
    """A new UniverseConfig field must be consciously placed: identity-relevant (hashed, the
    default) or analysis-only (NON_IDENTITY_UNIVERSE_FIELDS). Excluding a field changes the
    fingerprint of an existing lake; adding one to the hash forces an identity rebuild."""
    from chartlens_core.config import UniverseConfig
    from chartlens_pipeline.ingest import NON_IDENTITY_UNIVERSE_FIELDS

    hashed = {"exchange", "series", "history_target_years"}
    assert set(UniverseConfig.model_fields) == hashed | NON_IDENTITY_UNIVERSE_FIELDS


# ----------------------------------------------------------------------------- H2: quarantined sessions


def test_fully_rejected_session_is_not_counted_as_ingested(
    fake: FakeNse, lake: LocalObjectStore
) -> None:
    """H2 regression (the 2020-07-13 case): every row unparseable."""
    stamp = "22-JANUARY-2024"  # not a date format the parser accepts
    rows = "\n".join(f"S{i},EQ,10,12,9,11,11,10,100,1000,{stamp},1,INE002A01018," for i in range(5))
    fake.serve(*legacy_zip(MON, f"{HEADER}\n{rows}".encode()))
    svc = service(fake, lake)
    report = svc.backfill(MON, MON)
    m = manifest(lake, MON)
    assert m["status"] == DateStatus.QUARANTINED
    assert m["dq_condition"].startswith("ABNORMAL_QUARANTINE_RATIO: 5 of 5")
    assert report.metrics.dates_failed == 1 and report.metrics.dates_ingested == 0
    assert report.metrics.errors and "ABNORMAL_QUARANTINE_RATIO" in report.metrics.errors[0]
    # Retried from stored bytes on every run (no re-download) until a parser fix makes it pass.
    assert svc.plan_date(MON, refetch=False, reprocess=False) is DateAction.PROCESS_STORED


def test_partially_rejected_session_keeps_good_rows_but_is_flagged(
    fake: FakeNse, lake: LocalObjectStore
) -> None:
    bad = "BAD,EQ,10,5,9,11,11,10,100,1000,22-JAN-2024,1,INE144J01027,"
    fake.serve(*legacy_zip(MON, bhav(MON, BASE_ROWS) + f"\n{bad}".encode()))
    report = service(fake, lake).backfill(MON, MON)
    assert manifest(lake, MON)["status"] == DateStatus.QUARANTINED  # 1 of 3 > 5%
    assert canonical(lake, MON).num_rows == 2  # valid rows are still written
    assert report.metrics.quarantine_by_reason == {"INVALID_OHLC": 1}


def test_session_with_no_in_scope_rows_is_flagged(fake: FakeNse, lake: LocalObjectStore) -> None:
    fake.serve(*legacy_zip(MON, bhav(MON, [("BOND1", "N1", "INE002A01018", "100")])))
    service(fake, lake).backfill(MON, MON)
    assert manifest(lake, MON)["dq_condition"].startswith("NO_IN_SCOPE_ROWS")


def test_cli_exits_non_zero_for_a_quarantined_session(
    fake: FakeNse, lake: LocalObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from chartlens_pipeline import cli

    fake.serve(*legacy_zip(MON, bhav(MON, [("BOND1", "N1", "INE002A01018", "100")])))
    monkeypatch.setattr(cli, "_service", lambda _s, _e: service(fake, lake))
    result = CliRunner().invoke(cli.app, ["backfill", "--date", "2024-01-22"])
    assert result.exit_code == 2


# ----------------------------------------------------------------------------- daily catch-up


def daily_service(fake: FakeNse, lake: LocalObjectStore, today: date) -> IngestionService:
    settings = ChartLensSettings.model_construct()
    return IngestionService(
        settings,
        fake_provider(fake, CAL, settings),
        lake,
        overrides=IdentityOverrides(),
        today=lambda: today,
    )


def test_daily_catch_up_fills_sessions_missed_before_a_holiday(
    fake: FakeNse, lake: LocalObjectStore
) -> None:
    """Regression (first `daily` on main, 2026-10-02): the job ran on a holiday and only
    looked at that date, so the previous session (2026-10-01) was never ingested."""
    serve_week(fake)
    daily_service(fake, lake, MON).backfill(MON, MON)
    report = daily_service(fake, lake, FRI).catch_up()  # FRI is a holiday
    assert report.metrics.dates_ingested == 3  # TUE, WED, THU
    assert all(manifest(lake, d)["status"] == DateStatus.INGESTED for d in (TUE, WED, THU))
    assert (report.start, report.end) == (TUE, FRI)


def test_daily_catch_up_rechecks_a_recently_unpublished_session(
    fake: FakeNse, lake: LocalObjectStore
) -> None:
    """A file that appears late is picked up by the next daily run, even after a later
    session was ingested."""
    for day in (MON, WED, THU):
        fake.serve(*legacy_zip(day, bhav(day, BASE_ROWS)))
    daily_service(fake, lake, THU).backfill(MON, THU)
    assert manifest(lake, TUE)["status"] == DateStatus.NOT_PUBLISHED
    fake.serve(*legacy_zip(TUE, bhav(TUE, BASE_ROWS)))  # NSE publishes TUE late
    report = daily_service(fake, lake, FRI).catch_up()
    assert report.start == TUE and manifest(lake, TUE)["status"] == DateStatus.INGESTED


def test_daily_catch_up_skips_what_is_already_ingested(
    fake: FakeNse, lake: LocalObjectStore
) -> None:
    serve_week(fake)
    service(fake, lake).backfill(MON, THU)
    report = daily_service(fake, lake, FRI).catch_up()
    assert report.metrics.dates_ingested == 0 and report.metrics.errors == []


def test_daily_catch_up_refuses_an_empty_lake(fake: FakeNse, lake: LocalObjectStore) -> None:
    from chartlens_pipeline.ingest import NothingToCatchUp

    with pytest.raises(NothingToCatchUp, match="backfill first"):
        daily_service(fake, lake, FRI).catch_up()
