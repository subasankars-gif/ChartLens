"""NSE corporate-action subject grammar — every case is a real subject text from NSE's feed."""

from __future__ import annotations

from decimal import Decimal

import pytest

from chartlens_pipeline.corporate_actions_model import ActionClass, ComponentKind
from chartlens_pipeline.providers.nse.ca_subjects import interpret

D = Decimal
EQ, UQ, CASH, NONE, UNREC = (
    ActionClass.EQUITY_ADJUSTMENT,
    ActionClass.UNQUANTIFIED,
    ActionClass.CASH_DISTRIBUTION,
    ActionClass.NO_PRICE_EFFECT,
    ActionClass.UNRECOGNISED,
)


@pytest.mark.parametrize(
    ("subject", "fv_from", "fv_to"),
    [
        ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share", "10", "2"),
        ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share", "10", "1"),
        ("Fv Split Rs.10/- To Rs.2/", "10", "2"),
        ("Face Value Split From Rs.10/- To Rs.2/-", "10", "2"),
        ("Face Value Split From Rs 10 To Rs 5", "10", "5"),
        ("Face Valus Split (Sub-Division) - From Rs 10/- Per To Rs 2/- Per Share", "10", "2"),
        ("Fv Split Rs.10/- To Rs.2/Rd Date Revised", "10", "2"),
        ("Split-Rs.10tors.2/Div-60%Purpose Revised", "10", "2"),
        ("Fv Splt Frm Rs 10 To Rs 2", "10", "2"),
        ("Fv Split-Rs10/- To Rs5/-", "10", "5"),
        ("Face Value Split (Sub-Division) - From Rs 2 Per Share To Rs 1 Per Share", "2", "1"),
        ("Sub-Division From Rs 10/- Per Share To Rs 2/- Per Share", "10", "2"),
        ("Face Value Split From Rs.10/- To Rs.6/-", "10", "6"),
    ],
)
def test_face_value_splits(subject: str, fv_from: str, fv_to: str) -> None:
    r = interpret(subject)
    assert r.action_class is EQ
    (c,) = r.components
    assert (c.kind, c.fv_from, c.fv_to) == (ComponentKind.SPLIT, D(fv_from), D(fv_to))


@pytest.mark.parametrize(
    ("subject", "a", "b"),
    [
        ("Bonus 1:1", "1", "1"),
        ("Bonus 1 : 1", "1", "1"),
        ("Bonus - 1:2", "1", "2"),
        ("Bonus-1:1", "1", "1"),
        ("Bonus 13:10", "13", "10"),
        ("Bonus 1 : 1250", "1", "1250"),
        ("Bonus Shares In The Ratio Of 1:1", "1", "1"),
        ("Bonus Issue 1 : 1", "1", "1"),
        ("Intdiv-Rs 3persh/Bonus1:1", "1", "1"),
        ("Bonus 4:5 (Pursuant To Scheme Of Amalgamation)", "4", "5"),
        ("Annual General Meeting / Dividend - Rs 3/- Per Share / Bonus - 1:2", "1", "2"),
    ],
)
def test_equity_bonus(subject: str, a: str, b: str) -> None:
    r = interpret(subject)
    assert r.action_class is EQ
    (c,) = r.components
    assert (c.kind, c.ratio_new, c.ratio_held) == (ComponentKind.BONUS, D(a), D(b))


def test_bonus_and_split_together_yield_both_components() -> None:
    r = interpret("Bonus 1:1 / Face Value Split From Rs 10/- Per Share To Rs 2/- Per Share")
    assert r.action_class is EQ
    kinds = {c.kind: c for c in r.components}
    assert kinds[ComponentKind.BONUS].ratio_new == 1 and kinds[ComponentKind.SPLIT].fv_to == 2
    r2 = interpret("Fv Spl-Rs10Tors2/Bon-1:1")
    assert {c.kind for c in r2.components} == {ComponentKind.SPLIT, ComponentKind.BONUS}


@pytest.mark.parametrize(
    ("subject", "a", "b", "premium", "issue_price", "at_par"),
    [
        ("Rights 2:9 @ Premium Rs 7/-", "2", "9", "7", None, False),
        ("Rights 7:5 @ Premium Rs 8/- Per Share", "7", "5", "8", None, False),
        ("Rights 1:11.10 @ Premium Rs 0/-", "1", "11.10", "0", None, False),
        ("Agm/Rights 1:1 Prem@Rs.6 Purpose Revised", "1", "1", "6", None, False),
        ("Rights 2:5 @ Premium Of Rs 380 Per Share", "2", "5", "380", None, False),
        ("Rights 7:10 @ Prm Rs 102/-", "7", "10", "102", None, False),
        ("Rights At 2:1 At A Premium Of Rs.39.50 Per Share", "2", "1", "39.50", None, False),
        ("Rights-Eq 1:5@Prem Rs1580", "1", "5", "1580", None, False),
        ("Rigths-Eq 1:3@Prem Rs.75", "1", "3", "75", None, False),
        ("Rht 1:4@Prem Rs.5 Per Sh", "1", "4", "5", None, False),
        ("Rights 1:1 At Par", "1", "1", None, None, True),
        ("Rights - 2:3@ Par Rs 10 Per Share", "2", "3", None, "10", True),
        ("Rights 1:9 @ Premium 91", "1", "9", "91", None, False),
    ],
)
def test_priced_rights(
    subject: str, a: str, b: str, premium: str | None, issue_price: str | None, at_par: bool
) -> None:
    r = interpret(subject)
    assert r.action_class is EQ, r
    (c,) = r.components
    assert c.kind is ComponentKind.RIGHTS
    assert (c.ratio_new, c.ratio_held) == (D(a), D(b))
    assert c.premium == (D(premium) if premium else None)
    assert c.issue_price == (D(issue_price) if issue_price else None)
    assert c.at_par is at_par


@pytest.mark.parametrize(
    ("subject", "kind"),
    [
        ("Demerger", ComponentKind.UNQUANTIFIED_EVENT),
        ("Scheme Of Arrangement", ComponentKind.UNQUANTIFIED_EVENT),
        ("Sch Of Arngment-Demerger", ComponentKind.UNQUANTIFIED_EVENT),
        ("Capital Reduction Pursuant To Nclt Order", ComponentKind.UNQUANTIFIED_EVENT),
        ("Composite Scheme Of Amalgamation And Arrangement", ComponentKind.UNQUANTIFIED_EVENT),
        ("Merger/Demerger", ComponentKind.UNQUANTIFIED_EVENT),
        ("Bonus Ncrps 1:116", ComponentKind.NON_EQUITY_BONUS),
        ("Bonus Preference Shares 21:1", ComponentKind.NON_EQUITY_BONUS),
        ("Bonus 1 Dvr : 10 Eq Share", ComponentKind.NON_EQUITY_BONUS),
        ("Scheme Of Arrangement - Bonus Debentures 6:1", ComponentKind.NON_EQUITY_BONUS),
        ("Rights 1:1", ComponentKind.RIGHTS_UNPRICED),
        ("Agm/Div-50%/Rights-1:6", ComponentKind.RIGHTS_UNPRICED),
        (
            "Rights:14 Compulosry Convertible Debentures For Every 15 Equity Shares",
            ComponentKind.RIGHTS_COMPOSITE,
        ),
        (
            "Rights 1:50 @ Premium Rs 690/- With 17 Warrants For 50 Equity Shares",
            ComponentKind.RIGHTS_COMPOSITE,
        ),
        ("Rights 9:77 Partly Paid @ Premium Rs 100/-", ComponentKind.RIGHTS_COMPOSITE),
        ("Rhts Eq 1:5 & 1 Wrnt:5 Eq", ComponentKind.RIGHTS_COMPOSITE),
        (
            "Capital Reduction Rs 10 To Rs 3.30 / Consolidation Rs 3.30 To Rs.10",
            ComponentKind.UNQUANTIFIED_EVENT,
        ),
    ],
)
def test_unquantified_actions_are_never_given_a_factor(subject: str, kind: ComponentKind) -> None:
    r = interpret(subject)
    assert r.action_class is UQ
    assert kind in {c.kind for c in r.components}


def test_bonus_with_unpriced_rights_is_unquantified() -> None:
    """'Bonus - 1:5/Rights - 1:2': the bonus is known but the rights price is not, so the
    combined effect cannot be quantified."""
    r = interpret("Bonus - 1:5/Rights - 1:2")
    assert r.action_class is UQ
    assert {c.kind for c in r.components} == {ComponentKind.BONUS, ComponentKind.RIGHTS_UNPRICED}


@pytest.mark.parametrize(
    ("subject", "cls"),
    [
        ("Dividend - Rs 5 Per Share", CASH),
        ("Interim Dividend - Re 0.50 Per Share", CASH),
        ("Agm/Div Fin-20% + Spl-10%", CASH),  # "spl" = special dividend, not split
        ("Spl Dividend-Rs5/- Per Sh", CASH),
        ("Dividend - Rs 12.50 Per Share Pursuant To Scheme", CASH),
        ("Ann Bk Clsr/Dv-Re.1 Pr Sh", CASH),
        ("Interim Divdend - Rs 2 Per Share", CASH),
        ("To Distribute Net Proceeds From The Sale Of Rights Entitlement", CASH),
        ("Annual General Meeting", NONE),
        ("Annual General Meeing", NONE),
        ("Buy Back", NONE),
        ("Buy Back-Tender Offer", NONE),
        ("E-Voting", NONE),
        ("Election Of Directors", NONE),
        ("Interest Payment", CASH),
        ("General Corporate Purposes", UNREC),
        ("Cash-Out", UNREC),
        ("2 Ccds For Every 9 Equity Shares", UNREC),
    ],
)
def test_other_classes(subject: str, cls: ActionClass) -> None:
    assert interpret(subject).action_class is cls


def test_split_with_increasing_face_value_is_not_guessed() -> None:
    r = interpret("Face Value Split From Rs 2 To Rs 10")
    assert r.action_class is UNREC and r.notes


def test_interpretation_is_deterministic_and_versioned() -> None:
    s = "Bonus 1:1 / Face Value Split From Rs.10/- To Re.1/-"
    assert interpret(s) == interpret(s)
    assert interpret(s).grammar_version == "nse_ca_subject_v1"
