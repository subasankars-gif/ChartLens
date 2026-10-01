"""Adjustment engine: exact factors, price validation, the adjusted dataset and the
market-wide discontinuity guarantee (ADR-0011)."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import given
from hypothesis import strategies as st
from nse_fakes import FakeNse, calendar_for, fake_provider, legacy_zip

from chartlens_core.config import ChartLensSettings, NseProviderConfig, ProvidersConfig
from chartlens_pipeline.adjust import (
    AdjustmentService,
    CorporateActionOverrides,
    DiscontinuityReport,
    EventDecision,
    EventStatus,
    FactorOverride,
    Resolution,
    ResolvedAction,
    SecurityHistory,
    build_adjusted,
    component_factor,
    decide_security,
    face_value_at,
    tally_gaps,
)
from chartlens_pipeline.corporate_actions import CorporateActionStore
from chartlens_pipeline.corporate_actions_model import ActionClass, ActionComponent, ComponentKind
from chartlens_pipeline.identity import IdentityOverrides
from chartlens_pipeline.ingest import IngestionService
from chartlens_pipeline.providers.base import CorporateActionRecord
from chartlens_pipeline.providers.nse import NseProvider
from chartlens_pipeline.providers.nse.ca_subjects import interpret
from chartlens_pipeline.sources import SourceRecord
from chartlens_pipeline.storage import DataLakeLayout, LocalObjectStore

D, F = Decimal, Fraction
SETTINGS = ChartLensSettings.model_construct()
SRC = SourceRecord(
    source_id="nse:corporate_actions:test",
    exchange="NSE",
    source_type="corporate_actions",
    source_dataset="corporate_actions",
    source_date=date(2024, 1, 1),
    original_filename="ca.json",
    url="",
    content_hash="0" * 64,
    byte_size=0,
    downloaded_at=datetime(2024, 1, 1, tzinfo=UTC),
    mime_type="application/json",
    provider="nse",
    parser_version="test",
    storage_key="raw/x",
)


def days(n: int, start: date = date(2020, 1, 1)) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def hist(
    opens: Sequence[str | Decimal], closes: Sequence[str | Decimal], start: date = date(2020, 1, 1)
) -> SecurityHistory:
    o, c = [D(x) for x in opens], [D(x) for x in closes]
    n = len(o)
    return SecurityHistory(
        security_id="S1",
        dates=days(n, start),
        open=o,
        high=[max(a, b) for a, b in zip(o, c, strict=True)],
        low=[min(a, b) for a, b in zip(o, c, strict=True)],
        close=c,
        volume=[1000] * n,
        symbol=["ABC"] * n,
        series=["EQ"] * n,
        isin=["INE000A01011"] * n,
        source_hash=["h"] * n,
    )


def flat_with_jump(n: int, k: int, before: str, after: str) -> SecurityHistory:
    """Constant price ``before`` up to row k-1 and ``after`` from row k."""
    prices = [before] * k + [after] * (n - k)
    return hist(prices, prices)


def action(
    subject: str, ex: date, *, fv: str | None = "1", key: str | None = None
) -> ResolvedAction:
    rec = CorporateActionRecord(
        exchange="NSE",
        symbol="ABC",
        series="EQ",
        isin="INE000A01011",
        company="Abc Ltd",
        subject=subject,
        ex_date=ex,
        record_date=ex,
        face_value=D(fv) if fv else None,
        broadcast_at=None,
        record_key=key or f"{subject}@{ex}",
    )
    return ResolvedAction(rec, SRC, interpret(subject), "S1", Resolution.ISIN)


def decide(
    h: SecurityHistory | None,
    *acts: ResolvedAction,
    overrides: dict[date, FactorOverride] | None = None,
    end: date | None = None,
) -> list[EventDecision]:
    data_end = end if end is not None else (h.dates[-1] if h else None)
    return decide_security("S1", list(acts), h, overrides or {}, SETTINGS, data_end)


# ----------------------------------------------------------------------------- formulas


@pytest.mark.parametrize(
    ("component", "expected"),
    [
        (ActionComponent(ComponentKind.SPLIT, fv_from=D(10), fv_to=D(2)), F(1, 5)),
        (ActionComponent(ComponentKind.SPLIT, fv_from=D(10), fv_to=D("2.5")), F(1, 4)),
        (ActionComponent(ComponentKind.CONSOLIDATION, fv_from=D(1), fv_to=D(10)), F(10)),
        (ActionComponent(ComponentKind.BONUS, ratio_new=D(1), ratio_held=D(1)), F(1, 2)),
        (ActionComponent(ComponentKind.BONUS, ratio_new=D(1), ratio_held=D(2)), F(2, 3)),
        (ActionComponent(ComponentKind.BONUS, ratio_new=D(13), ratio_held=D(10)), F(10, 23)),
    ],
)
def test_share_count_factors_are_exact(component: ActionComponent, expected: Fraction) -> None:
    assert component_factor(component, None, None)[0] == expected


def test_rights_factor_is_terp_over_cum_close() -> None:
    # 1:5 at premium 90 on face value 10 → issue 100; cum close 200;
    # TERP = (5·200 + 1·100) / 6; factor = TERP / 200 = 11/12.
    c = ActionComponent(ComponentKind.RIGHTS, ratio_new=D(1), ratio_held=D(5), premium=D(90))
    f, inputs, notes = component_factor(c, D(200), D(10))
    assert f == F(11, 12) and notes == []
    assert inputs["issue_price"] == "100" and inputs["cum_rights_close"] == "200"


def test_rights_priced_at_or_above_market_is_no_adjustment() -> None:
    c = ActionComponent(ComponentKind.RIGHTS, ratio_new=D(1), ratio_held=D(1), issue_price=D(250))
    assert component_factor(c, D(200), None)[0] == 1


def test_rights_without_known_face_value_is_not_computed() -> None:
    c = ActionComponent(ComponentKind.RIGHTS, ratio_new=D(1), ratio_held=D(1), premium=D(5))
    f, _, notes = component_factor(c, D(200), None)
    assert f is None and "face value" in notes[0]


def test_face_value_is_reconstructed_backwards_from_the_current_value() -> None:
    s1 = ActionComponent(ComponentKind.SPLIT, fv_from=D(10), fv_to=D(2))
    s2 = ActionComponent(ComponentKind.SPLIT, fv_from=D(2), fv_to=D(1))
    changes = [(date(2015, 6, 1), s1), (date(2020, 6, 1), s2)]
    assert face_value_at(date(2014, 1, 1), D(1), changes, [])[0] == 10
    assert face_value_at(date(2016, 1, 1), D(1), changes, [])[0] == 2
    assert face_value_at(date(2021, 1, 1), D(1), changes, [])[0] == 1
    assert face_value_at(date(2015, 6, 1), D(1), changes, [])[0] == 2  # on the ex-date: before it
    fv, notes = face_value_at(date(2014, 1, 1), D(5), changes, [])
    assert fv == 10 and "mismatch" in notes[0]  # chain inconsistency is reported
    assert face_value_at(date(2014, 1, 1), D(1), changes, [date(2018, 1, 1)])[0] is None


# ----------------------------------------------------------------------------- decisions


def test_split_matching_the_ex_date_gap_is_verified_and_applied() -> None:
    h = flat_with_jump(30, 20, "100", "20")
    (d,) = decide(h, action("Face Value Split From Rs 10 To Rs 2", h.dates[20]))
    assert (d.status, d.factor, d.applied, d.breaks_continuity) == (
        EventStatus.VERIFIED,
        F(1, 5),
        True,
        False,
    )
    assert d.boundary_date == h.dates[20] and d.residual == pytest.approx(0)
    series = build_adjusted("NSE", h, [d])
    rows = series.table.to_pylist()
    assert rows[0]["adj_close"] == D("20.000000") and rows[0]["close"] == D(100)
    assert rows[0]["price_factor"] == "1/5" and rows[0]["volume_factor"] == "5/1"
    assert rows[0]["adj_volume"] == D(5000) and rows[0]["next_factor_date"] == h.dates[20]
    assert rows[20]["adj_close"] == D(20) and rows[20]["price_factor"] == "1/1"
    assert not any(r["break_before"] for r in rows)
    report = DiscontinuityReport(threshold=0.25)
    assert tally_gaps(report, h, series, [d], []) == []
    assert (report.raw_large_gaps, report.resolved_by_adjustment, report.adjusted_large_gaps) == (
        1,
        1,
        0,
    )


def test_ex_date_on_a_non_trading_day_uses_the_next_session() -> None:
    h = flat_with_jump(30, 20, "100", "50")
    h.dates[20:] = [d + timedelta(days=3) for d in h.dates[20:]]  # a long weekend before row 20
    (d,) = decide(h, action("Bonus 1:1", h.dates[19] + timedelta(days=2)))
    assert d.status is EventStatus.VERIFIED and d.boundary_date == h.dates[20]


def test_factor_without_a_matching_gap_is_rejected_not_applied() -> None:
    h = flat_with_jump(30, 20, "100", "100")  # no price change at all
    (d,) = decide(h, action("Bonus 1:1", h.dates[20]))
    assert d.status is EventStatus.REJECTED_BY_PRICE and not d.applied
    assert not d.breaks_continuity  # nothing discontinuous in the data
    assert all(r["price_factor"] == "1/1" for r in build_adjusted("NSE", h, [d]).table.to_pylist())


def test_factor_fitting_the_next_session_is_reported_never_moved() -> None:
    h = flat_with_jump(30, 21, "100", "50")  # gap is one session after the stated ex-date
    (d,) = decide(h, action("Bonus 1:1", h.dates[20]))
    assert d.status is EventStatus.REJECTED_BY_PRICE
    assert any("ex-date may be wrong" in n for n in d.notes)
    assert d.factor == F(1, 2) and not d.applied


def test_partial_fit_is_applied_but_suspect() -> None:
    h = flat_with_jump(30, 20, "100", "35")  # 1:1 bonus explains −69%, observed −105% (log)
    (d,) = decide(h, action("Bonus 1:1", h.dates[20]))
    assert d.status is EventStatus.SUSPECT and d.applied
    assert d.residual is not None and abs(d.residual) > 0.15


def test_unquantified_event_is_a_hard_break_with_no_factor() -> None:
    h = flat_with_jump(30, 20, "100", "60")
    (d,) = decide(h, action("Demerger", h.dates[20]))
    assert (d.status, d.factor, d.breaks_continuity) == (EventStatus.UNQUANTIFIED, None, True)
    rows = build_adjusted("NSE", h, [d]).table.to_pylist()
    assert [r["trading_date"] for r in rows if r["break_before"]] == [h.dates[20]]
    assert all(r["adj_close"] == r["close"] for r in rows)


def test_unquantified_event_breaks_even_without_a_visible_gap() -> None:
    """Never convert an unexplained observation into an assumed fact: a small gap does not
    prove the scheme had no effect."""
    h = flat_with_jump(30, 20, "100", "100")
    (d,) = decide(h, action("Scheme Of Arrangement", h.dates[20]))
    assert d.breaks_continuity


def test_equity_terms_combined_with_unquantified_terms_are_unquantified() -> None:
    h = flat_with_jump(30, 20, "100", "50")
    (d,) = decide(h, action("Bonus 1:1", h.dates[20]), action("Demerger", h.dates[20]))
    assert d.status is EventStatus.UNQUANTIFIED and d.action_class is ActionClass.UNQUANTIFIED


def test_disagreeing_records_are_not_resolved_by_guessing() -> None:
    h = flat_with_jump(30, 20, "100", "50")
    (d,) = decide(h, action("Bonus 1:1", h.dates[20]), action("Bonus 1:2", h.dates[20]))
    assert d.status is EventStatus.CONFLICTING_RECORDS and d.breaks_continuity


def test_duplicate_records_with_identical_terms_are_one_event() -> None:
    h = flat_with_jump(30, 20, "100", "50")
    (d,) = decide(
        h, action("Bonus 1:1", h.dates[20], key="a"), action("Bonus 1 : 1", h.dates[20], key="b")
    )
    assert d.status is EventStatus.VERIFIED and d.record_keys == ["a", "b"]


def test_bonus_and_split_on_one_day_multiply() -> None:
    h = flat_with_jump(30, 20, "100", "10")
    (d,) = decide(h, action("Bonus 1:1 / Face Value Split From Rs 10 To Rs 2", h.dates[20]))
    assert d.factor == F(1, 10) and d.status is EventStatus.VERIFIED


def test_two_ex_dates_on_one_session_are_validated_jointly() -> None:
    h = flat_with_jump(30, 20, "100", "50")
    h.dates[20] = h.dates[19] + timedelta(days=5)  # sessions 19 → 20 span several days
    h.dates[21:] = [h.dates[20] + timedelta(days=i + 1) for i in range(len(h.dates) - 21)]
    a = action("Bonus 1:1", h.dates[19] + timedelta(days=1))
    b = action("Bonus 1:1", h.dates[19] + timedelta(days=3))  # a duplicate on another date
    decisions = decide(h, a, b)
    assert {d.status for d in decisions} == {EventStatus.REJECTED_BY_PRICE}  # ¼ overshoots ½


def test_rights_use_the_reconstructed_face_value_and_cum_close() -> None:
    h = flat_with_jump(30, 20, "200", str(D(200) * 11 / 12))
    acts = [
        action("Rights 1:5 @ Premium Rs 90/-", h.dates[20], fv="2"),
        action("Face Value Split From Rs 10 To Rs 2", h.dates[25] + timedelta(days=400), fv="2"),
    ]
    decisions = decide(h, *acts, end=h.dates[25] + timedelta(days=500))
    rights = next(d for d in decisions if d.ex_date == h.dates[20])
    assert rights.factor == F(11, 12) and rights.inputs["face_value"] == "10"
    # ln(11/12) = -0.087 is inside the 0.15 noise floor: applied, but not "verified".
    assert rights.status is EventStatus.CONSISTENT and rights.applied


def test_factor_smaller_than_noise_is_applied_when_the_gap_stays_within_noise() -> None:
    h = flat_with_jump(30, 20, "100", "101")  # gap +1%; a 1:10 bonus is ln(10/11) = -0.095
    (d,) = decide(h, action("Bonus 1:10", h.dates[20]))
    assert d.status is EventStatus.CONSISTENT and d.applied
    assert d.notes == []  # no ex-date speculation when prices cannot discriminate


def test_factor_smaller_than_noise_never_creates_a_large_gap() -> None:
    h = flat_with_jump(30, 20, "100", "114")  # +13% gap; 1:10 bonus would leave ~+23.6%
    (d,) = decide(h, action("Bonus 1:10", h.dates[20]))
    assert d.status is EventStatus.REJECTED_BY_PRICE and not d.applied


def test_event_after_the_data_end_is_pending() -> None:
    h = flat_with_jump(30, 20, "100", "100")
    (d,) = decide(h, action("Bonus 1:1", h.dates[-1] + timedelta(days=10)))
    assert d.status is EventStatus.PENDING and not d.applied and not d.breaks_continuity


def test_event_without_trades_on_both_sides_is_not_applicable() -> None:
    h = flat_with_jump(30, 20, "100", "100")
    (d,) = decide(h, action("Bonus 1:1", h.dates[0]), end=h.dates[-1])
    assert d.status is EventStatus.NOT_APPLICABLE and not d.applied
    (d2,) = decide(None, action("Bonus 1:1", h.dates[5]), end=h.dates[-1])
    assert d2.status is EventStatus.NOT_APPLICABLE


def test_reviewed_override_supplies_a_factor_for_an_unquantified_event() -> None:
    h = flat_with_jump(30, 20, "100", "25")
    o = FactorOverride("INE000A01011", h.dates[20], F(1, 4), "scheme document p.4", "reviewer")
    (d,) = decide(h, action("Demerger", h.dates[20]), overrides={h.dates[20]: o})
    assert (d.method, d.status, d.factor, d.breaks_continuity) == (
        "OVERRIDE",
        EventStatus.VERIFIED,
        F(1, 4),
        False,
    )
    assert d.inputs["evidence"] == "scheme document p.4"


def test_override_of_one_records_a_reviewed_no_effect() -> None:
    h = flat_with_jump(30, 20, "100", "101")
    o = FactorOverride("INE000A01011", h.dates[20], F(1), "acquirer in amalgamation", "reviewer")
    (d,) = decide(h, action("Scheme Of Amalgamation", h.dates[20]), overrides={h.dates[20]: o})
    assert d.status is EventStatus.NO_ADJUSTMENT and not d.breaks_continuity


def test_suppressed_records_are_ignored() -> None:
    h = flat_with_jump(30, 20, "100", "50")
    a = action("Bonus 1:1", h.dates[20])
    a.suppressed = "wrong security"
    assert decide(h, a) == []


def test_cash_and_meeting_records_make_no_event() -> None:
    h = flat_with_jump(30, 20, "100", "100")
    assert (
        decide(
            h,
            action("Dividend - Rs 5 Per Share", h.dates[20]),
            action("Annual General Meeting", h.dates[10]),
        )
        == []
    )


def test_overrides_file_requires_evidence(tmp_path: Path) -> None:
    p = tmp_path / "nse.toml"
    p.write_text(
        '[[factor]]\nisin = "INE1"\nex_date = 2021-10-22\nfactor = "1/10"\nreviewed_by = "x"\n'
    )
    with pytest.raises(ValueError, match="evidence"):
        CorporateActionOverrides.load(p)
    p.write_text(
        '[[factor]]\nisin = "ine1"\nex_date = 2021-10-22\nfactor = "1/10"\nevidence = "order"\n'
        'reviewed_by = "x"\n[[suppress]]\nrecord_key = "k"\nreason = "dup"\n'
    )
    o = CorporateActionOverrides.load(p)
    assert o.factors[("INE1", date(2021, 10, 22))].factor == F(1, 10)
    assert o.suppress == {"k": "dup"} and len(o.fingerprint) == 12
    assert CorporateActionOverrides.load(tmp_path / "missing.toml").fingerprint == "none"


# ----------------------------------------------------------------------------- report


def test_report_counts_a_forced_bad_factor_as_a_new_gap() -> None:
    """The report is an independent check: a factor applied without validation shows up."""
    h = flat_with_jump(30, 20, "100", "100")
    d = EventDecision(
        "S1",
        h.dates[20],
        ActionClass.EQUITY_ADJUSTMENT,
        [],
        [],
        [],
        "BONUS",
        F(1, 2),
        {},
        EventStatus.VERIFIED,
        boundary_date=h.dates[20],
    )
    report = DiscontinuityReport(threshold=0.25)
    tally_gaps(report, h, build_adjusted("NSE", h, [d]), [d], [])
    assert report.new_gaps_introduced == 1 and not report.hard_requirements_met


def test_report_classifies_remaining_gaps() -> None:
    h = hist(
        ["100", "100", "50", "50", "20", "20", "60", "60"],
        ["100", "100", "50", "50", "20", "20", "60", "60"],
    )
    decisions = decide(h, action("Demerger", h.dates[2]))
    report = DiscontinuityReport(threshold=0.25)
    unexplained = tally_gaps(
        report, h, build_adjusted("NSE", h, decisions), decisions, [h.dates[4]]
    )
    assert report.raw_large_gaps == 3 and report.adjusted_large_gaps == 3
    assert report.at_unquantified_events == 1 and report.at_cash_distributions == 1
    (gap,) = unexplained
    assert report.unexplained == 1 and (gap.trading_date, gap.session_index) == (h.dates[6], 6)
    assert gap.gap == pytest.approx(2.0) and gap.prev_close == D(20)
    assert report.hard_requirements_met


# ----------------------------------------------------------------------------- the guarantee


_TRUE_FACTORS = [F(1, 2), F(1, 5), F(2, 3), F(1, 10), F(10), F(10, 11)]
_SUBJECTS = {
    F(1, 2): "Bonus 1:1",
    F(1, 5): "Face Value Split From Rs 10 To Rs 2",
    F(2, 3): "Bonus 1:2",
    F(1, 10): "Face Value Split From Rs 10 To Re 1",
    F(10): "Consolidation From Rs 1 To Rs 10",
    F(10, 11): "Bonus 1:10",  # smaller than the noise floor
}


@st.composite
def scenario(draw: st.DrawFn) -> tuple[SecurityHistory, list[ResolvedAction]]:
    n = draw(st.integers(30, 120))
    rets = draw(st.lists(st.floats(-0.08, 0.08), min_size=n, max_size=n))
    gaps = draw(st.lists(st.floats(-0.05, 0.05), min_size=n, max_size=n))
    real = draw(
        st.lists(st.tuples(st.integers(1, n - 1), st.sampled_from(_TRUE_FACTORS)), max_size=3)
    )
    shock = draw(st.lists(st.tuples(st.integers(1, n - 1), st.floats(-0.9, 1.5)), max_size=2))
    shocks = dict(shock)
    opens: list[Decimal] = []
    closes: list[Decimal] = []
    c = 500.0
    for i in range(n):
        o = c * math.exp(gaps[i]) * (1 + shocks.get(i, 0)) if i else c
        if i and any(k == i for k, _ in real):
            o *= math.prod(float(f) for k, f in real if k == i)
        c = max(o * math.exp(rets[i]), 1.0)
        o = max(o, 1.0)
        opens.append(D(f"{o:.2f}"))
        closes.append(D(f"{c:.2f}"))
    h = hist(opens, closes)
    acts: list[ResolvedAction] = []
    for k, f in real:
        if draw(st.booleans()):  # the feed reports it, sometimes on the wrong date
            k2 = min(max(k + draw(st.sampled_from([0, 0, 0, -1, 1, 3])), 1), n - 1)
            acts.append(action(_SUBJECTS[f], h.dates[k2], key=f"r{k}{f}"))
    for _ in range(draw(st.integers(0, 3))):  # records with no basis in the prices
        k = draw(st.integers(0, n + 5))
        subject = draw(st.sampled_from([*_SUBJECTS.values(), "Demerger", "Dividend - Rs 2"]))
        acts.append(action(subject, h.dates[0] + timedelta(days=k), key=f"s{k}{subject}"))
    return h, acts


@given(scenario())
def test_no_adjustment_ever_creates_or_worsens_a_large_gap(
    case: tuple[SecurityHistory, list[ResolvedAction]],
) -> None:
    h, acts = case
    decisions = decide(h, *acts)
    series = build_adjusted("NSE", h, decisions)
    report = DiscontinuityReport(threshold=0.25)
    tally_gaps(report, h, series, decisions, [])
    assert report.new_gaps_introduced == 0, report.new_gap_examples
    assert report.gaps_worsened == 0, report.new_gap_examples
    for d in decisions:  # applied ⇒ the ex-date gap shrank, or stayed within noise (req. 5/6)
        if d.applied:
            assert d.residual is not None and d.raw_gap is not None and d.tolerance is not None
            if d.status is EventStatus.CONSISTENT:
                assert abs(d.residual) <= min(d.tolerance, math.log1p(0.25))
            else:
                assert abs(d.residual) < abs(d.raw_gap)
    # Adjusted = raw × exact cumulative factor, rounded once (requirement 4).
    for row in series.table.to_pylist():
        num, den = map(int, row["price_factor"].split("/"))
        exact = F(row["close"]) * F(num, den)
        assert abs(F(row["adj_close"]) - exact) <= F(1, 2_000_000)


# ----------------------------------------------------------------------------- service, end to end

HEADER = "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,"
SESSIONS = [d for d in days(40, date(2024, 1, 1)) if d.weekday() < 5]
EX = SESSIONS[15]


def bhav(day: date, rows: list[tuple[str, str, str]]) -> bytes:
    stamp = day.strftime("%d-%b-%Y").upper()
    lines = [HEADER]
    for sym, isin, close in rows:
        c = D(close)
        lines.append(f"{sym},EQ,{c},{c},{c},{c},{c},{c},1000,{c * 1000},{stamp},10,{isin},")
    return "\n".join(lines).encode()


def ca_json(items: list[tuple[str, str, str, date, str]]) -> bytes:
    return json.dumps(
        [
            {
                "symbol": sym,
                "series": "EQ",
                "isin": isin,
                "comp": sym.title(),
                "subject": subject,
                "exDate": ex.strftime("%d-%b-%Y"),
                "recDate": ex.strftime("%d-%b-%Y"),
                "faceVal": fv,
                "caBroadcastDate": None,
            }
            for sym, isin, subject, ex, fv in items
        ]
    ).encode()


def build_lake(
    tmp_path: Path,
) -> tuple[ChartLensSettings, NseProvider, LocalObjectStore, Callable[[], date]]:
    """Three securities over ~6 weeks of 2024 plus a corporate-action feed:
    SPLITCO 10→2 split (prices ÷5), DEMERCO demerger (−30%), PLAIN dividend only, and a
    feed record for an ISIN the master has never seen."""
    settings = ChartLensSettings.model_construct(
        providers=ProvidersConfig(
            nse=NseProviderConfig(corporate_actions_first_month=date(2024, 1, 1))
        )
    )
    fake = FakeNse()
    for i, day in enumerate(SESSIONS):
        split = i >= 15
        fake.serve(
            *legacy_zip(
                day,
                bhav(
                    day,
                    [
                        ("SPLITCO", "INE002A01018", "40" if split else "200"),
                        ("DEMERCO", "INE467B01029", "70" if split else "100"),
                        ("PLAIN", "INE009A01021", "50"),
                    ],
                ),
            )
        )
    provider = fake_provider(fake, calendar_for([2024]), settings)
    lake = LocalObjectStore(tmp_path / "lake")
    IngestionService(
        settings, provider, lake, overrides=IdentityOverrides(), today=lambda: date(2024, 3, 1)
    ).backfill(SESSIONS[0], SESSIONS[-1])
    feed = ca_json(
        [
            ("SPLITCO", "INE002A01018", "Face Value Split From Rs 10 To Rs 2", EX, "2"),
            ("DEMERCO", "INE467B01029", "Demerger", EX, "1"),
            ("PLAIN", "INE009A01021", "Dividend - Rs 2 Per Share", EX, "10"),
            ("GHOST", "INE999A01011", "Bonus 1:1", EX, "10"),
        ]
    )
    for window in provider.corporate_actions.windows(date(2024, 1, 1), date(2024, 2, 29)):
        body = feed if window[0].month == EX.month else b"[]"
        fake.serve(provider.corporate_actions.url(window), body)

    def today() -> date:
        return date(2024, 2, 20)

    fetch = CorporateActionStore("NSE", provider.corporate_actions, lake, today=today).fetch(
        date(2024, 1, 1), date(2024, 2, 29)
    )
    assert fetch.failed == [] and fetch.downloaded == 2
    return settings, provider, lake, today


def test_service_end_to_end_is_reproducible(tmp_path: Path) -> None:
    settings, provider, lake, today = build_lake(tmp_path)
    svc = AdjustmentService(
        settings, provider, lake, overrides=CorporateActionOverrides(), today=today
    )
    result = svc.run()
    assert result.published and result.report.hard_requirements_met
    by_status = {d.status for d in result.decisions}
    assert by_status == {EventStatus.VERIFIED, EventStatus.UNQUANTIFIED}
    assert result.counts["records_by_resolution"]["UNRESOLVED"] == 1  # GHOST: never guessed
    assert result.report.resolved_by_adjustment == 1
    assert result.report.at_unquantified_events == 1  # DEMERCO −30%: kept, flagged

    manifest = json.loads(lake.get(DataLakeLayout.adjusted_manifest_key("NSE")))
    assert manifest["adjustment_version"] == result.adjustment_version
    assert len(manifest["files"]) == 3
    split_sid = next(d.security_id for d in result.decisions if d.status is EventStatus.VERIFIED)
    table = pq.read_table(
        pa.BufferReader(lake.get(DataLakeLayout.adjusted_daily_key("NSE", split_sid)))
    )
    rows = table.to_pylist()
    assert {r["adj_close"] for r in rows} == {D(40)}
    assert {r["close"] for r in rows} == {D(200), D(40)}  # raw kept beside adjusted
    events = pq.read_table(pa.BufferReader(lake.get(DataLakeLayout.adjustment_events_key("NSE"))))
    assert events.num_rows == 2
    report = json.loads(lake.get(DataLakeLayout.adjustment_report_key("NSE")))
    assert report["hard_requirements"]["new_gaps_introduced"] == 0

    before = {k: lake.get(k) for k in lake.list(DataLakeLayout.adjusted_daily_prefix("NSE"))}
    again = AdjustmentService(
        settings, provider, lake, overrides=CorporateActionOverrides(), today=today
    ).run()
    assert again.adjustment_version == result.adjustment_version
    assert again.counts["files_written"] == 0
    assert {
        k: lake.get(k) for k in lake.list(DataLakeLayout.adjusted_daily_prefix("NSE"))
    } == before


def test_override_for_an_unknown_isin_blocks_publication(tmp_path: Path) -> None:
    settings = ChartLensSettings.model_construct(
        providers=ProvidersConfig(
            nse=NseProviderConfig(corporate_actions_first_month=date(2024, 1, 1))
        )
    )
    fake = FakeNse()
    for day in SESSIONS[:3]:
        fake.serve(*legacy_zip(day, bhav(day, [("PLAIN", "INE009A01021", "50")])))
    provider = fake_provider(fake, calendar_for([2024]), settings)
    lake = LocalObjectStore(tmp_path / "lake")
    IngestionService(
        settings, provider, lake, overrides=IdentityOverrides(), today=lambda: date(2024, 3, 1)
    ).backfill(SESSIONS[0], SESSIONS[2])
    o = FactorOverride("INE000X01011", SESSIONS[1], F(1, 2), "doc", "me")
    svc = AdjustmentService(
        settings,
        provider,
        lake,
        overrides=CorporateActionOverrides({(o.isin, o.ex_date): o}),
        today=lambda: date(2024, 1, 10),
    )
    result = svc.run()
    assert not result.published and result.unresolved_overrides
    assert not lake.exists(DataLakeLayout.adjusted_manifest_key("NSE"))
