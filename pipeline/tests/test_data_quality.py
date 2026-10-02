"""Data quality: findings, continuity breaks and usable_from (ADR-0012)."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from nse_fakes import calendar_for
from test_adjust import EX, SESSIONS, build_lake

from chartlens_core.config import ChartLensSettings
from chartlens_core.domain import DataQualityStatus as Q
from chartlens_core.quality import Severity
from chartlens_pipeline.adjust import AdjustmentService, CorporateActionOverrides
from chartlens_pipeline.data_quality import (
    AdjustedDataNotPublished,
    DataQualityService,
    SecuritySeries,
    market_findings,
    security_findings,
)
from chartlens_pipeline.identity import IdentityOverrides
from chartlens_pipeline.storage import DataLakeLayout

SETTINGS = ChartLensSettings.model_construct()
CAL = calendar_for([2024])
EXPECTED = CAL.expected_sessions(date(2024, 1, 1), date(2024, 12, 31))


def series(dates: list[date], closes: list[float]) -> SecuritySeries:
    return SecuritySeries("S1", dates, closes, "ABC", "INE002A01018", "h")


def event(
    status: str, boundary: date | None, breaks: bool, factor: str | None = None
) -> dict[str, Any]:
    return {
        "status": status,
        "ex_date": boundary,
        "boundary_date": boundary,
        "breaks_continuity": breaks,
        "subjects": json.dumps(["x"]),
        "factor": factor,
    }


def codes(findings: list[Any]) -> list[str]:
    return [f.code for f in findings]


def test_event_statuses_become_findings() -> None:
    d = EXPECTED[:30]
    s = series(d, [100.0] * 30)
    out = security_findings(
        s,
        [
            event("VERIFIED", d[5], False, "1/5"),
            event("UNQUANTIFIED", d[10], True),
            event("SUSPECT", d[15], False, "1/2"),
            event("NOT_APPLICABLE", None, False),
        ],
        [],
        [],
        EXPECTED,
        SETTINGS,
    )
    by = {f.code: f for f in out}
    assert by["FACTOR_APPLIED"].severity is Severity.INFO
    assert by["UNQUANTIFIED_ACTION"].breaks_continuity and by["UNQUANTIFIED_ACTION"].start == d[10]
    assert by["FACTOR_SUSPECT"].severity is Severity.WARN
    assert "NOT_APPLICABLE" not in str(codes(out))


def test_large_move_is_a_warning_unless_an_action_explains_it() -> None:
    d = EXPECTED[:10]
    closes = [100.0] * 5 + [50.0] * 5
    assert codes(security_findings(series(d, closes), [], [], [], EXPECTED, SETTINGS)) == [
        "UNEXPLAINED_MOVE"
    ]
    cash = {"ex_date": d[5], "action_class": "CASH_DISTRIBUTION", "suppressed_reason": None}
    assert security_findings(series(d, closes), [], [cash], [], EXPECTED, SETTINGS) == []
    unq = [event("UNQUANTIFIED", d[5], True)]
    assert codes(security_findings(series(d, closes), unq, [], [], EXPECTED, SETTINGS)) == [
        "UNQUANTIFIED_ACTION"
    ]


def test_long_trading_gap_breaks_continuity_short_one_does_not() -> None:
    d = EXPECTED[:10] + EXPECTED[100:110]  # 90 sessions without a trade
    out = security_findings(series(d, [100.0] * 20), [], [], [], EXPECTED, SETTINGS)
    gap = next(f for f in out if f.code == "TRADING_GAP")
    assert gap.breaks_continuity and gap.start == EXPECTED[100]
    assert "MISSING_SESSIONS" in codes(out)
    d2 = EXPECTED[:10] + EXPECTED[40:50]
    out2 = security_findings(series(d2, [100.0] * 20), [], [], [], EXPECTED, SETTINGS)
    assert "TRADING_GAP" not in codes(out2)


def test_identifier_changes_are_information() -> None:
    d = EXPECTED[:10]
    ids = [
        {
            "identifier_type": "SYMBOL",
            "identifier_value": "OLD",
            "valid_from": d[0],
            "evidence": "x",
        },
        {
            "identifier_type": "SYMBOL",
            "identifier_value": "NEW",
            "valid_from": d[5],
            "evidence": "y",
        },
    ]
    (f,) = security_findings(series(d, [100.0] * 10), [], [], ids, EXPECTED, SETTINGS)
    assert (f.code, f.severity, f.detail) == ("SYMBOL_CHANGE", Severity.INFO, "OLD → NEW")


def test_market_findings_flag_sessions_not_ingested_and_derived_years() -> None:
    days = EXPECTED[:5]
    manifests = {d: {"status": "INGESTED"} for d in days}
    manifests[days[2]] = {"status": "QUARANTINED"}
    del manifests[days[3]]
    out, expected = market_findings(manifests, CAL)
    assert expected == days
    assert [(f.code, f.start) for f in out] == [
        ("SESSION_QUARANTINED", days[2]),
        ("SESSION_MISSING", days[3]),
    ]
    assert market_findings({}, CAL) == ([], [])


def test_service_end_to_end(tmp_path: Path) -> None:
    settings, provider, lake, today = build_lake(tmp_path)
    with pytest.raises(AdjustedDataNotPublished):
        DataQualityService(settings, provider, lake, identity_overrides=IdentityOverrides()).run()
    AdjustmentService(
        settings, provider, lake, overrides=CorporateActionOverrides(), today=today
    ).run()
    result = DataQualityService(
        settings, provider, lake, identity_overrides=IdentityOverrides()
    ).run()
    by_symbol = {s["symbol"]: s for s in result.statuses}
    assert by_symbol["SPLITCO"]["usable_from"] == SESSIONS[0]
    assert by_symbol["SPLITCO"]["status"] == Q.USABLE
    assert by_symbol["DEMERCO"]["usable_from"] == EX  # hard break at the demerger
    assert by_symbol["DEMERCO"]["usable_sessions"] == len(SESSIONS) - 15
    assert by_symbol["PLAIN"]["status"] == Q.USABLE
    assert {s["instrument_type"] for s in result.statuses} == {"EQUITY_SHARE"}
    assert all(s["analytical_universe"] for s in result.statuses)
    # the unknown-ISIN record is surfaced market-wide, never attached to a security
    ghost = [f for f in result.findings if f.code == "ACTION_UNRESOLVED"]
    assert len(ghost) == 1 and ghost[0].security_id is None
    status = pq.read_table(pa.BufferReader(lake.get(DataLakeLayout.data_quality_status_key("NSE"))))
    assert status.num_rows == 3
    report = json.loads(lake.get(DataLakeLayout.data_quality_report_key("NSE")))
    assert report["securities_with_usable_from_after_first_date"] == 1
    assert report["breaks_by_code"] == {"UNQUANTIFIED_ACTION": 1}
    assert result.as_of == SESSIONS[-1] and SESSIONS[-1] - timedelta(days=60) < EX

    # The continuity regime: DEMERCO splits at the demerger, the others are one segment.
    segs = pq.read_table(
        pa.BufferReader(lake.get(DataLakeLayout.continuity_segments_key("NSE")))
    ).to_pylist()
    by_sid = {s["security_id"]: s["symbol"] for s in result.statuses}
    demer = [g for g in segs if by_sid[g["security_id"]] == "DEMERCO"]
    assert [(g["segment_start"], g["segment_end"], g["sessions"], g["cause"]) for g in demer] == [
        (SESSIONS[0], SESSIONS[14], 15, "FIRST_SESSION"),
        (EX, SESSIONS[-1], len(SESSIONS) - 15, "UNQUANTIFIED_ACTION"),
    ]
    assert demer[1]["continuity_segment_id"] == f"{demer[1]['security_id']}@{EX.isoformat()}"
    assert len(segs) == 4 and result.segments == segs


# ----------------------------------------------------------------------------- decisions 2026-10-02


def test_instrument_type_comes_from_the_isin() -> None:
    from chartlens_pipeline.providers.nse import NseIdentityPolicy

    p = NseIdentityPolicy()
    assert p.instrument_type(["INE002A01018"]) == "EQUITY_SHARE"
    assert p.instrument_type(["IN9155A01020"]) == "EQUITY_SHARE"  # DVR
    assert p.instrument_type(["INF204KB17I5"]) == "FUND_UNIT"  # ETF
    assert p.instrument_type(["INE002A20018"]) == "RIGHTS_ENTITLEMENT"
    assert p.instrument_type(["INE002A01018", "INE002A02016"]) == "OTHER_02"  # latest wins
    assert p.instrument_type([]) == "UNKNOWN"  # never guessed


def gap_series(prices: list[tuple[float, float]]) -> SecuritySeries:
    d = EXPECTED[: len(prices)]
    opens = [o for o, _ in prices]
    closes = [c for _, c in prices]
    return SecuritySeries("S1", d, closes, "ABC", "INE002A01018", "h", opens, closes)


def test_unexplained_gap_beyond_half_breaks_continuity_but_is_not_adjusted() -> None:
    s = gap_series([(100, 100)] * 5 + [(45, 45)] * 5)  # -55% overnight
    out = security_findings(s, [], [], [], EXPECTED, SETTINGS)
    brk = [f for f in out if f.code == "UNEXPLAINED_PRICE_DISCONTINUITY"]
    assert len(brk) == 1 and brk[0].breaks_continuity and brk[0].start == EXPECTED[5]
    assert "not adjusted" in brk[0].detail


def test_gap_rule_respects_threshold_reference_price_and_universe() -> None:
    within = gap_series([(100, 100)] * 5 + [(55, 55)] * 5)  # -45%: warning only
    assert "UNEXPLAINED_PRICE_DISCONTINUITY" not in codes(
        security_findings(within, [], [], [], EXPECTED, SETTINGS)
    )
    penny = gap_series([(1.5, 1.5)] * 5 + [(0.5, 0.5)] * 5)  # below Rs 2: tick moves
    assert "UNEXPLAINED_PRICE_DISCONTINUITY" not in codes(
        security_findings(penny, [], [], [], EXPECTED, SETTINGS)
    )
    big = gap_series([(100, 100)] * 5 + [(45, 45)] * 5)
    etf = security_findings(big, [], [], [], EXPECTED, SETTINGS, analytical=False)
    assert "UNEXPLAINED_PRICE_DISCONTINUITY" not in codes(etf)


def test_gap_already_explained_by_a_break_is_not_counted_twice() -> None:
    s = gap_series([(100, 100)] * 5 + [(45, 45)] * 5)
    unq = [event("UNQUANTIFIED", EXPECTED[5], True)]
    out = security_findings(s, unq, [], [], EXPECTED, SETTINGS)
    assert [f.code for f in out if f.breaks_continuity] == ["UNQUANTIFIED_ACTION"]
