"""Security identity resolution (ADR-0009)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from chartlens_core.config import IdentityConfig
from chartlens_pipeline.daily import QuarantineReason
from chartlens_pipeline.identity import (
    Evidence,
    IdentifierType,
    IdentityBasis,
    IdentityOverrides,
    ListingStatus,
    Observation,
    Resolution,
    SecurityMaster,
    StatusOverride,
    SymbolChangeNotice,
    security_id_for,
)
from chartlens_pipeline.providers.nse import NseIdentityPolicy

CFG = IdentityConfig()
POLICY = NseIdentityPolicy()
D1, D2, D3 = date(2024, 1, 10), date(2024, 1, 11), date(2024, 1, 12)

RELIANCE = "INE002A01018"
TCS = "INE467B01029"
THREE_I_OLD, THREE_I_NEW = "INE748C01020", "INE748C01038"  # real ISINs, same issuer code 748C


def resolve(
    master: SecurityMaster,
    day: date,
    *obs: Observation,
    notices: tuple[SymbolChangeNotice, ...] = (),
    overrides: IdentityOverrides | None = None,
    anchored: bool = True,
    config: IdentityConfig = CFG,
) -> Resolution:
    return master.resolve(
        obs,
        day,
        source_id=f"src:{day}",
        policy=POLICY,
        config=config,
        notices=notices,
        overrides=overrides,
        anchored=anchored,
    )


def o(n: int, symbol: str, isin: str | None, series: str = "EQ") -> Observation:
    return Observation(n, symbol, series, isin)


def only_id(res: Resolution) -> str:
    assert len(set(res.assigned.values())) == 1, res
    return next(iter(res.assigned.values()))


# ----------------------------------------------------------------------------- ISIN-bearing rows


def test_new_security_gets_deterministic_id_and_identifier_spans() -> None:
    m = SecurityMaster("NSE")
    res = resolve(m, D1, o(1, "RELIANCE", RELIANCE))
    sid = only_id(res)
    assert sid == security_id_for("NSE", IdentityBasis.ISIN, RELIANCE)
    assert res.created == {sid}
    assert m.current(sid, IdentifierType.SYMBOL) == "RELIANCE"
    assert m.current(sid, IdentifierType.ISIN) == RELIANCE
    assert m.current(sid, IdentifierType.SERIES) == "EQ"
    assert m.securities[sid].identity_basis is IdentityBasis.ISIN


def test_existing_security_is_reused_and_span_extended() -> None:
    m = SecurityMaster("NSE")
    sid = only_id(resolve(m, D1, o(1, "RELIANCE", RELIANCE)))
    res = resolve(m, D2, o(1, "RELIANCE", RELIANCE))
    assert only_id(res) == sid and res.created == set() and res.updated == {sid}
    (span,) = m.spans_for(sid, IdentifierType.SYMBOL)
    assert (span.valid_from, span.valid_to) == (D1, D2)


def test_symbol_change_keeps_the_same_security() -> None:
    m = SecurityMaster("NSE")
    sid = only_id(resolve(m, D1, o(1, "OLDNAME", TCS)))
    assert only_id(resolve(m, D2, o(1, "NEWNAME", TCS))) == sid
    spans = m.spans_for(sid, IdentifierType.SYMBOL)
    assert [(s.value, s.valid_from, s.valid_to) for s in spans] == [
        ("OLDNAME", D1, D1),
        ("NEWNAME", D2, D2),
    ]
    assert m.current(sid, IdentifierType.SYMBOL) == "NEWNAME"


@pytest.mark.parametrize(("first", "second"), [("EQ", "BE"), ("BE", "EQ")])
def test_series_move_keeps_the_same_security(first: str, second: str) -> None:
    m = SecurityMaster("NSE")
    sid = only_id(resolve(m, D1, o(1, "ABC", TCS, first)))
    assert only_id(resolve(m, D2, o(1, "ABC", TCS, second))) == sid
    assert [s.value for s in m.spans_for(sid, IdentifierType.SERIES)] == [first, second]
    assert m.current(sid, IdentifierType.SERIES) == second
    assert len(m.securities) == 1


def test_unknown_isin_with_free_symbol_is_a_new_security() -> None:
    m = SecurityMaster("NSE")
    a = only_id(resolve(m, D1, o(1, "AAA", RELIANCE)))
    b = only_id(resolve(m, D2, o(1, "BBB", TCS)))
    assert a != b and len(m.securities) == 2


def test_new_isin_on_a_symbol_held_that_day_is_a_conflict() -> None:
    m = SecurityMaster("NSE")
    resolve(m, D1, o(1, "ABC", RELIANCE))
    res = resolve(m, D1, o(2, "ABC", TCS))  # a second file version claiming ABC for another ISIN
    assert res.assigned == {}
    assert res.quarantined[2][0] is QuarantineReason.IDENTITY_CONFLICT
    assert len(m.securities) == 1


def test_two_isins_claiming_one_symbol_in_one_file_are_both_quarantined() -> None:
    m = SecurityMaster("NSE")
    res = resolve(m, D1, o(1, "ABC", RELIANCE), o(2, "ABC", TCS))
    assert res.assigned == {}
    assert {r for r, _ in res.quarantined.values()} == {QuarantineReason.IDENTITY_CONFLICT}
    assert m.securities == {}


def test_same_isin_in_two_series_on_one_day_is_quarantined() -> None:
    m = SecurityMaster("NSE")
    res = resolve(m, D1, o(1, "ABC", TCS, "EQ"), o(2, "ABC", TCS, "BE"))
    assert res.assigned == {}
    assert {r for r, _ in res.quarantined.values()} == {QuarantineReason.DUPLICATE_SECURITY_DATE}


def test_one_isin_under_two_symbols_on_one_day_is_never_split() -> None:
    """Regression (found by the property test): one row could link to an existing
    security while its twin created a new one, putting one ISIN on two securities."""
    m = SecurityMaster("NSE")
    old = only_id(resolve(m, D1, o(1, "3IINFOTECH", THREE_I_OLD)))
    res = resolve(m, D2, o(1, "3IINFOTECH", THREE_I_NEW), o(2, "OTHER", THREE_I_NEW))
    assert res.assigned == {}
    assert {r for r, _ in res.quarantined.values()} == {QuarantineReason.DUPLICATE_SECURITY_DATE}
    assert m.by_isin(THREE_I_NEW) == set() and m.by_isin(THREE_I_OLD) == {old}


def test_known_isin_whose_symbol_is_held_by_another_security_is_a_conflict() -> None:
    m = SecurityMaster("NSE")
    resolve(m, D1, o(1, "AAA", RELIANCE), o(2, "BBB", TCS))
    res = resolve(m, D1, o(3, "AAA", TCS))
    assert res.quarantined[3][0] is QuarantineReason.IDENTITY_CONFLICT


# ---------------------------------------------------------------------- ISIN re-issue (same issuer)


def test_reissued_isin_with_same_symbol_links_to_the_same_security() -> None:
    m = SecurityMaster("NSE")
    sid = only_id(resolve(m, D1, o(1, "3IINFOTECH", THREE_I_OLD)))
    res = resolve(m, D2, o(1, "3IINFOTECH", THREE_I_NEW))
    assert only_id(res) == sid and res.created == set()
    assert "SAME_ISSUER_ISIN" in res.links[0]["evidence"]
    isins = m.spans_for(sid, IdentifierType.ISIN)
    assert [(s.value, s.evidence) for s in isins] == [
        (THREE_I_OLD, Evidence.OBSERVED),
        (THREE_I_NEW, Evidence.SAME_ISSUER_ISIN),
    ]


def test_reissued_isin_plus_symbol_change_links_only_with_a_notice() -> None:
    """3i Infotech: INE748C01020/3IINFOTECH → INE748C01038/3IINFOLTD. Both identifiers
    change; only the exchange's symbol-change notice connects them."""
    notice = SymbolChangeNotice("3IINFOTECH", "3IINFOLTD", D2, "3i Infotech Limited")

    with_notice = SecurityMaster("NSE")
    sid = only_id(resolve(with_notice, D1, o(1, "3IINFOTECH", THREE_I_OLD)))
    res = resolve(with_notice, D2, o(1, "3IINFOLTD", THREE_I_NEW), notices=(notice,))
    assert only_id(res) == sid
    assert "SYMBOL_CHANGE_NOTICE" in res.links[0]["evidence"]

    without = SecurityMaster("NSE")
    old = only_id(resolve(without, D1, o(1, "3IINFOTECH", THREE_I_OLD)))
    new = only_id(resolve(without, D2, o(1, "3IINFOLTD", THREE_I_NEW)))
    assert new != old  # no evidence → a separate security, never a guessed merge


def test_different_issuer_reusing_a_symbol_after_a_gap_is_a_new_security() -> None:
    m = SecurityMaster("NSE")
    old = only_id(resolve(m, D1, o(1, "ABC", RELIANCE)))
    later = D1 + timedelta(days=400)
    new = only_id(resolve(m, later, o(1, "ABC", TCS)))
    assert new != old and m.current(new, IdentifierType.SYMBOL) == "ABC"


def test_same_issuer_match_to_two_securities_is_a_conflict() -> None:
    m = SecurityMaster("NSE")
    # Two securities of issuer 748C/01 both using the symbol near D2 (only possible with bad data).
    resolve(m, D1, o(1, "X", "INE748C01012"))
    resolve(m, D1, o(1, "X2", THREE_I_OLD))
    resolve(m, D2, o(1, "X2", THREE_I_OLD), o(2, "X", "INE748C01012"))
    res = resolve(m, D3, o(1, "X", THREE_I_NEW), notices=(SymbolChangeNotice("X2", "X", D3, "co"),))
    assert res.quarantined[1][0] is QuarantineReason.IDENTITY_CONFLICT


def test_same_issuer_rule_can_be_disabled() -> None:
    m = SecurityMaster("NSE")
    old = only_id(resolve(m, D1, o(1, "3IINFOTECH", THREE_I_OLD)))
    res = resolve(
        m,
        D1 + timedelta(days=400),
        o(1, "3IINFOTECH", THREE_I_NEW),
        config=IdentityConfig(link_same_issuer_isin=False),
    )
    assert only_id(res) != old


# ----------------------------------------------------------------------------- rows without ISIN


def test_no_isin_rows_wait_for_an_anchor() -> None:
    m = SecurityMaster("NSE")
    res = resolve(m, D1, o(1, "ABC", None), anchored=False)
    assert res.quarantined[1][0] is QuarantineReason.UNRESOLVED_IDENTITY
    assert m.securities == {}


def test_no_isin_row_resolves_to_the_later_isin_security_by_continuity() -> None:
    """Descending backfill: the ISIN era (D2) is ingested first, then the pre-ISIN day D1."""
    m = SecurityMaster("NSE")
    sid = only_id(resolve(m, D2, o(1, "ABC", RELIANCE)))
    res = resolve(m, D1, o(1, "ABC", None))
    assert only_id(res) == sid and res.created == set()
    spans = m.spans_for(sid, IdentifierType.SYMBOL)
    assert [(s.evidence, s.valid_from) for s in spans] == [
        (Evidence.SYMBOL_CONTINUITY, D1),
        (Evidence.OBSERVED, D2),
    ]


def test_no_isin_row_beyond_the_gap_becomes_its_own_security() -> None:
    m = SecurityMaster("NSE")
    anchor = only_id(resolve(m, D2, o(1, "ABC", RELIANCE)))
    far = D2 - timedelta(days=CFG.max_symbol_gap_days + 1)
    res = resolve(m, far, o(1, "ABC", None))
    other = only_id(res)
    assert other != anchor
    assert m.securities[other].identity_basis is IdentityBasis.SYMBOL_CONTINUITY
    assert other == security_id_for("NSE", IdentityBasis.SYMBOL_CONTINUITY, f"ABC:{far}")


def test_no_isin_row_follows_a_symbol_change_notice_backwards() -> None:
    m = SecurityMaster("NSE")
    sid = only_id(resolve(m, D2, o(1, "NEWSYM", RELIANCE)))
    notice = SymbolChangeNotice("OLDSYM", "NEWSYM", D2, "Co Ltd")
    res = resolve(m, D1, o(1, "OLDSYM", None), notices=(notice,))
    assert only_id(res) == sid
    assert m.spans_for(sid, IdentifierType.SYMBOL)[0].evidence is Evidence.SYMBOL_CHANGE_NOTICE


def test_no_isin_row_matching_two_securities_is_ambiguous() -> None:
    m = SecurityMaster("NSE")
    resolve(m, D1 - timedelta(days=10), o(1, "ABC", RELIANCE))
    resolve(m, D1 + timedelta(days=10), o(1, "ABC", TCS))
    res = resolve(m, D1, o(1, "ABC", None))
    assert res.quarantined[1][0] is QuarantineReason.AMBIGUOUS_IDENTITY


def test_no_isin_resolution_can_be_disabled() -> None:
    m = SecurityMaster("NSE")
    res = resolve(m, D1, o(1, "ABC", None), config=IdentityConfig(resolve_without_isin=False))
    assert res.quarantined[1][0] is QuarantineReason.UNRESOLVED_IDENTITY


# ---------------------------------------------------------------------------- overrides & lifecycle


def test_override_links_an_isin_that_rules_would_not() -> None:
    m = SecurityMaster("NSE")
    sid = only_id(resolve(m, D1, o(1, "AAA", RELIANCE)))
    ov = IdentityOverrides(link_isin={TCS: RELIANCE})
    res = resolve(m, D2, o(1, "BBB", TCS), overrides=ov)
    assert only_id(res) == sid and res.links[0]["evidence"].startswith("OVERRIDE")


@pytest.mark.parametrize("direction", ["old_to_new", "new_to_old"])
def test_override_links_regardless_of_which_isin_is_seen_first(direction: str) -> None:
    """H1 regression: overrides are symmetric and beat the known-ISIN rule, so a descending
    build links 3i Infotech whichever way round the reviewed entry is written."""
    links = {THREE_I_OLD: THREE_I_NEW} if direction == "old_to_new" else {THREE_I_NEW: THREE_I_OLD}
    ov = IdentityOverrides(link_isin=links)
    m = SecurityMaster("NSE")
    new = only_id(resolve(m, date(2021, 10, 22), o(1, "3IINFOLTD", THREE_I_NEW), overrides=ov))
    res = resolve(m, date(2021, 8, 27), o(1, "3IINFOTECH", THREE_I_OLD), overrides=ov)
    assert only_id(res) == new and len(m.securities) == 1
    assert res.links[0]["evidence"].startswith("OVERRIDE")
    assert m.spans_for(new, IdentifierType.ISIN)[0].evidence is Evidence.OVERRIDE


def test_override_with_unseen_partner_creates_the_security_normally() -> None:
    m = SecurityMaster("NSE")
    res = resolve(m, D1, o(1, "BBB", TCS), overrides=IdentityOverrides(link_isin={TCS: RELIANCE}))
    assert res.created == {only_id(res)}


def test_override_contradicting_the_existing_master_is_a_conflict_not_a_silent_merge() -> None:
    """If a master was built without the override, an override cannot quietly re-point an
    already-assigned ISIN; the ingestion guard requires a rebuild instead."""
    m = SecurityMaster("NSE")
    resolve(m, D1, o(1, "AAA", RELIANCE), o(2, "BBB", TCS))
    res = resolve(m, D2, o(1, "BBB", TCS), overrides=IdentityOverrides(link_isin={TCS: RELIANCE}))
    assert res.quarantined[1][0] is QuarantineReason.IDENTITY_CONFLICT
    assert "rebuilt" in res.quarantined[1][1]


def test_distinct_override_blocks_same_issuer_linking() -> None:
    m = SecurityMaster("NSE")
    old = only_id(resolve(m, D1, o(1, "3IINFOTECH", THREE_I_OLD)))
    ov = IdentityOverrides(distinct_isin=frozenset({THREE_I_NEW}))
    new = only_id(
        resolve(m, D1 + timedelta(days=400), o(1, "3IINFOTECH", THREE_I_NEW), overrides=ov)
    )
    assert new != old


def test_absence_alone_never_marks_a_security_delisted() -> None:
    m = SecurityMaster("NSE")
    a = only_id(resolve(m, D1, o(1, "AAA", RELIANCE)))
    b = only_id(resolve(m, D3, o(1, "BBB", TCS)))
    m.update_listing_status(active_cutoff=D2, as_of=D3)
    assert m.securities[a].listing_status is ListingStatus.INACTIVE
    assert m.securities[b].listing_status is ListingStatus.ACTIVE
    forced = IdentityOverrides(
        status={RELIANCE: StatusOverride(ListingStatus.DELISTED, D2, "exchange notice")}
    )
    m.update_listing_status(active_cutoff=D2, as_of=D3, overrides=forced)
    assert m.securities[a].listing_status is ListingStatus.DELISTED


def test_overrides_load_from_toml(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "nse.toml"
    path.write_text(
        f'[[link_isin]]\nisin = "{TCS}"\nexisting_isin = "{RELIANCE}"\nreason = "test"\n'
        f'[[distinct_isin]]\nisin = "{THREE_I_NEW}"\nreason = "test"\n'
        f'[[status]]\nisin = "{RELIANCE}"\nstatus = "DELISTED"\neffective = 2024-01-01\nreason = "test"\n'
    )
    ov = IdentityOverrides.load(path)
    assert ov.link_isin == {TCS: RELIANCE} and THREE_I_NEW in ov.distinct_isin
    assert ov.status[RELIANCE].status is ListingStatus.DELISTED and ov.fingerprint != "none"
    assert IdentityOverrides.load(tmp_path / "missing.toml").fingerprint == "none"


# ------------------------------------------------------------------------ persistence & determinism


def _history() -> list[tuple[date, list[Observation]]]:
    return [
        (D1, [o(1, "RELIANCE", RELIANCE), o(2, "3IINFOTECH", THREE_I_OLD), o(3, "OLD", TCS, "BE")]),
        (D2, [o(1, "RELIANCE", RELIANCE), o(2, "3IINFOTECH", THREE_I_NEW), o(3, "NEW", TCS, "EQ")]),
    ]


def test_same_inputs_produce_identical_masters() -> None:
    tables = []
    for _ in range(2):
        m = SecurityMaster("NSE")
        for day, obs in _history():
            resolve(m, day, *obs)
        tables.append(m.to_tables())
    assert tables[0][0].equals(tables[1][0]) and tables[0][1].equals(tables[1][1])


def test_row_order_within_a_file_does_not_matter() -> None:
    a, b = SecurityMaster("NSE"), SecurityMaster("NSE")
    for day, obs in _history():
        resolve(a, day, *obs)
        resolve(b, day, *reversed(obs))
    assert a.to_tables()[1].equals(b.to_tables()[1])


def test_persistence_round_trip() -> None:
    m = SecurityMaster("NSE")
    for day, obs in _history():
        resolve(m, day, *obs)
    securities, history = m.to_tables()
    again = SecurityMaster.from_tables("NSE", securities, history)
    s2, h2 = again.to_tables()
    assert securities.equals(s2) and history.equals(h2)


# ----------------------------------------------------------------------------- properties

SYMBOLS = ["AAA", "BBB", "CCC", "DDD"]
ISINS = [RELIANCE, TCS, THREE_I_OLD, THREE_I_NEW, "INE144J01027", None]


@given(
    st.lists(
        st.lists(
            st.tuples(
                st.sampled_from(SYMBOLS), st.sampled_from(ISINS), st.sampled_from(["EQ", "BE"])
            ),
            min_size=1,
            max_size=6,
        ),
        min_size=1,
        max_size=8,
    ),
    st.booleans(),
)
def test_resolution_invariants_hold_for_arbitrary_histories(
    days: list[list[tuple[str, str | None, str]]], descending: bool
) -> None:
    m = SecurityMaster("NSE")
    dated = [(D1 + timedelta(days=i), rows) for i, rows in enumerate(days)]
    for day, rows in sorted(dated, reverse=descending):
        obs = [Observation(n, sym, series, isin) for n, (sym, isin, series) in enumerate(rows)]
        res = resolve(m, day, *obs)
        assigned = list(res.assigned.values())
        # one security/date → at most one canonical row
        assert len(assigned) == len(set(assigned))
        # every row is either assigned or quarantined, never both, never lost
        assert set(res.assigned).isdisjoint(res.quarantined)
        assert set(res.assigned) | set(res.quarantined) == {x.row_number for x in obs}
        # one symbol → one security on a given day
        by_symbol: dict[str, set[str]] = {}
        for n, sid in res.assigned.items():
            by_symbol.setdefault(obs[n].symbol, set()).add(sid)
        assert all(len(v) == 1 for v in by_symbol.values())
    for span in m.spans():
        assert span.valid_from <= span.valid_to
    # every ISIN belongs to exactly one security
    for isin in {i for i in ISINS if i}:
        assert len(m.by_isin(isin)) <= 1


def test_override_fingerprint_tracks_decisions_not_wording(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "nse.toml"
    entry = '[[link_isin]]\nisin = "{a}"\nexisting_isin = "{b}"\nreason = "{r}"\n'
    path.write_text(entry.format(a=TCS, b=RELIANCE, r="first wording"))
    first = IdentityOverrides.load(path)
    path.write_text("# comment\n" + entry.format(a=TCS, b=RELIANCE, r="corrected evidence"))
    reworded = IdentityOverrides.load(path)
    assert first.fingerprint == reworded.fingerprint  # no identity rebuild for wording
    assert first.document_hash != reworded.document_hash  # but outputs quoting it change
    path.write_text(entry.format(a=THREE_I_NEW, b=RELIANCE, r="x"))
    assert IdentityOverrides.load(path).fingerprint != first.fingerprint
