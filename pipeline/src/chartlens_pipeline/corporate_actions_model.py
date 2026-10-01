"""Exchange-neutral corporate-action interpretation model (ADR-0011).

Providers turn their exchange's free-text descriptions into these types; everything
downstream (factors, validation, data quality) works only with them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class ActionClass(StrEnum):
    EQUITY_ADJUSTMENT = "EQUITY_ADJUSTMENT"
    """Quantified change to the equity share count: split, consolidation, bonus, priced rights."""
    UNQUANTIFIED = "UNQUANTIFIED"
    """Price-relevant, but the source does not quantify it (demerger, scheme, capital
    reduction, non-equity bonus, unpriced/composite rights). Never adjusted: a hard
    discontinuity, with ``usable_from`` after it, unless a reviewed override supplies a
    factor from a primary document."""
    CASH_DISTRIBUTION = "CASH_DISTRIBUTION"
    """Dividends and similar. Never adjusted: prices are shown as traded (ADR-0005)."""
    NO_PRICE_EFFECT = "NO_PRICE_EFFECT"
    """Meetings, buybacks and other announcements with no mechanical price effect."""
    UNRECOGNISED = "UNRECOGNISED"
    """The grammar could not read it. Never guessed; surfaced by data quality for review."""


class ComponentKind(StrEnum):
    SPLIT = "SPLIT"
    CONSOLIDATION = "CONSOLIDATION"
    BONUS = "BONUS"
    RIGHTS = "RIGHTS"
    NON_EQUITY_BONUS = "NON_EQUITY_BONUS"
    RIGHTS_UNPRICED = "RIGHTS_UNPRICED"
    RIGHTS_COMPOSITE = "RIGHTS_COMPOSITE"
    UNQUANTIFIED_EVENT = "UNQUANTIFIED_EVENT"
    UNRECOGNISED = "UNRECOGNISED"


@dataclass(frozen=True)
class ActionComponent:
    kind: ComponentKind
    ratio_new: Decimal | None = None
    """a in "a:b" — new shares issued..."""
    ratio_held: Decimal | None = None
    """b in "a:b" — ...for every b held."""
    fv_from: Decimal | None = None
    fv_to: Decimal | None = None
    premium: Decimal | None = None
    """Rights premium over face value (issue price = face value at the time + premium)."""
    issue_price: Decimal | None = None
    """Explicit rights issue price, when the source states one."""
    at_par: bool = False
    text: str = ""
    """The matched fragment of the source text, for audit."""


@dataclass(frozen=True)
class SubjectInterpretation:
    action_class: ActionClass
    components: tuple[ActionComponent, ...]
    notes: tuple[str, ...]
    grammar_version: str
