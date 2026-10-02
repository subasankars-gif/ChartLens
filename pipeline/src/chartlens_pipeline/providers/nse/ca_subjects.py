"""NSE corporate-action subject grammar (ADR-0011).

NSE describes each action in free text (``subject``). This module turns that text into
exchange-neutral :class:`ActionComponent`s. The grammar was built from the full feed
(41,293 records, 2006–2026; 828 distinct price-relevant subjects), not from memory.

Rules, in order:

1. **Unquantified wins.** If the subject mentions a demerger, scheme, amalgamation,
   merger, reconstruction or capital reduction (outside a "pursuant to …" phrase), a
   bonus of a non-equity instrument (NCRPS, debentures, preference shares, DVRs), or a
   rights issue with no price or with other instruments (NCDs, CCPS, warrants,
   partly-paid shares), the action is ``UNQUANTIFIED``: no factor, hard discontinuity.
2. Otherwise priced equity components — face-value split/consolidation, equity bonus,
   rights with premium or at par — make it ``EQUITY_ADJUSTMENT``.
3. Otherwise cash distributions (dividends, interest) are ``CASH_DISTRIBUTION``
   (never adjusted), and meetings/buybacks ``NO_PRICE_EFFECT``.
4. Anything mentioning split/bonus/rights/consolidation that no rule could read, or
   any subject matching nothing at all, is ``UNRECOGNISED`` — never guessed.

Pure functions; versioned by ``SUBJECT_GRAMMAR_VERSION``.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Final

from chartlens_pipeline.corporate_actions_model import (
    ActionClass,
    ActionComponent,
    ComponentKind,
    SubjectInterpretation,
)

SUBJECT_GRAMMAR_VERSION: Final = "nse_ca_subject_v1"

_NUM = r"(\d+(?:\.\d+)?)"
_MONEY = r"(?:rs|re)?\s*\.?\s*" + _NUM

_PURSUANT = re.compile(r"\(?\s*pursuant\s+to\s+(?:the\s+)?(?:scheme|nclt)[^)/]*\)?")
_UNQUANTIFIED = re.compile(
    r"de-?merg|\bsch(?:eme)?\b|\bsheme\b|\bsch\s+of\b|arrangement|arangement|arng|amalgam"
    r"|\bmerger\b|reconstruction|capital\s+reduction|reduction\s+of\s+capital"
)
_NON_EQUITY = r"ncrps|crps|preference|\bpref\b|debenture|\bdeb\b|\bncd|\bbond|\bdvr\b|dif\s*vot"
_RIGHTS_COMPOSITE = re.compile(
    r"ccps|pccps|\bncd|\bpcd|\bbond|debenture|\bccd|warrant|\bwrnt|\bwar\b|zccb|partly\s+paid"
    r"|dif\s*vot|\bdvr\b|\bfcd"
)

_SPLIT = re.compile(
    r"(?:f(?:ace)?\s*v(?:alue|alus)?\s*spl(?:i?t)?|\bsplit|\bsplt|sub[\s-]?division|\bspl\b)"
    r"[^0-9]{0,60}?" + _NUM + r"[^0-9]{0,40}?to\s*(?:face\s*value\s*)?" + _MONEY
)
_CONSOLIDATION = re.compile(r"consolidat\w*[^0-9]{0,60}?" + _NUM + r"[^0-9]{0,40}?to\s*" + _MONEY)
_BONUS_WORD = r"\bbon(?:us)?(?![a-z])"
_BONUS = re.compile(
    _BONUS_WORD + r"(?P<mid>[^0-9/]{0,40}?)(?P<a>\d+(?:\.\d+)?)\s*(?P<inst>[a-z ]{0,20}?)\s*:"
    r"\s*(?P<b>\d+(?:\.\d+)?)"
)
_RIGHTS_WORD = r"\b(?:rights?|rigths?|rigts?|rgts?|rhts?|rhgts?|rght|rht)(?![a-z])"
_RIGHTS = re.compile(
    _RIGHTS_WORD + r"(?P<mid>[^0-9/]{0,30}?)(?P<a>\d+(?:\.\d+)?)\s*:\s*(?P<b>\d+(?:\.\d+)?)"
    r"(?P<tail>[^/]{0,70})"
)
_PREMIUM = re.compile(r"pr(?:e)?m(?:ium)?\s*(?:of\s*)?@?\s*" + _MONEY)
_PAR = re.compile(r"(?:\bat\s+|@\s*)par\b(?:\s*" + _MONEY + r")?")
_ISSUE_PRICE = re.compile(r"@\s*(?:rs|re)\.?\s*" + _NUM)

# "spl" alone means *special* (dividend) far more often than split; it only counts as a
# split inside _SPLIT, where face values "from … to …" must follow.
_PRICE_WORDS = re.compile(
    r"split|\bsplt\b|sub[\s-]?div|consolidat|" + _BONUS_WORD + "|" + _RIGHTS_WORD
)
_CASH = re.compile(
    r"\bdiv|intdiv|\bdv[-.]|interest|distribution|return\s+(?:of|on)\s+capital"
    r"|sale\s+of\s+rights\s+entitlement|\bspecial\b|\bspl\b"
)
_NO_EFFECT = re.compile(
    r"general\s+mee|\bagm\b|\begm\b|buy\s*-?\s*back|postal\s+ballot|closure|voting"
    r"|record\s+date|redemption|\bmeeting\b|nterest|\belect|elctn|director"
)


def _d(text: str | None) -> Decimal | None:
    return Decimal(text) if text else None


def interpret(subject: str) -> SubjectInterpretation:
    text = " ".join(subject.lower().split())
    notes: list[str] = []
    components: list[ActionComponent] = []

    scan = _PURSUANT.sub(" ", text)
    if _UNQUANTIFIED.search(scan):
        word = _UNQUANTIFIED.search(scan)
        components.append(
            ActionComponent(ComponentKind.UNQUANTIFIED_EVENT, text=word.group(0) if word else "")
        )

    for m in _SPLIT.finditer(text):
        fv_from, fv_to = Decimal(m.group(1)), Decimal(m.group(2))
        if fv_to < fv_from:
            components.append(
                ActionComponent(ComponentKind.SPLIT, fv_from=fv_from, fv_to=fv_to, text=m.group(0))
            )
        else:
            notes.append(f"split text with non-decreasing face value: {m.group(0)!r}")
            components.append(ActionComponent(ComponentKind.UNRECOGNISED, text=m.group(0)))
    for m in _CONSOLIDATION.finditer(text):
        fv_from, fv_to = Decimal(m.group(1)), Decimal(m.group(2))
        if fv_to > fv_from:
            components.append(
                ActionComponent(
                    ComponentKind.CONSOLIDATION, fv_from=fv_from, fv_to=fv_to, text=m.group(0)
                )
            )
        else:
            components.append(ActionComponent(ComponentKind.UNRECOGNISED, text=m.group(0)))
    for m in _BONUS.finditer(text):
        a, b = Decimal(m.group("a")), Decimal(m.group("b"))
        if re.search(_NON_EQUITY, m.group("mid") + " " + m.group("inst")):
            components.append(
                ActionComponent(
                    ComponentKind.NON_EQUITY_BONUS, ratio_new=a, ratio_held=b, text=m.group(0)
                )
            )
        elif a > 0 and b > 0:
            components.append(
                ActionComponent(ComponentKind.BONUS, ratio_new=a, ratio_held=b, text=m.group(0))
            )
    if (
        re.search(_BONUS_WORD, text)
        and re.search(_NON_EQUITY, text)
        and not any(
            c.kind in (ComponentKind.BONUS, ComponentKind.NON_EQUITY_BONUS) for c in components
        )
    ):
        components.append(
            ActionComponent(ComponentKind.NON_EQUITY_BONUS, text="bonus of non-equity instrument")
        )

    rights_text = re.search(_RIGHTS_WORD, text) and "sale of rights entitlement" not in text
    if rights_text:
        if _RIGHTS_COMPOSITE.search(text):
            components.append(
                ActionComponent(
                    ComponentKind.RIGHTS_COMPOSITE, text="rights with other instruments"
                )
            )
        else:
            found = False
            for m in _RIGHTS.finditer(text):
                found = True
                a, b, tail = Decimal(m.group("a")), Decimal(m.group("b")), m.group("tail")
                premium = _PREMIUM.search(tail)
                par = _PAR.search(tail)
                issue = _ISSUE_PRICE.search(tail)
                if premium:
                    components.append(
                        ActionComponent(
                            ComponentKind.RIGHTS,
                            ratio_new=a,
                            ratio_held=b,
                            premium=Decimal(premium.group(1)),
                            text=m.group(0),
                        )
                    )
                elif par:
                    components.append(
                        ActionComponent(
                            ComponentKind.RIGHTS,
                            ratio_new=a,
                            ratio_held=b,
                            at_par=True,
                            issue_price=_d(par.group(1)),
                            text=m.group(0),
                        )
                    )
                elif issue:
                    components.append(
                        ActionComponent(
                            ComponentKind.RIGHTS,
                            ratio_new=a,
                            ratio_held=b,
                            issue_price=Decimal(issue.group(1)),
                            text=m.group(0),
                        )
                    )
                else:
                    components.append(
                        ActionComponent(
                            ComponentKind.RIGHTS_UNPRICED,
                            ratio_new=a,
                            ratio_held=b,
                            text=m.group(0),
                        )
                    )
            if not found:
                components.append(
                    ActionComponent(
                        ComponentKind.UNRECOGNISED, text="rights without a readable ratio"
                    )
                )

    kinds = {c.kind for c in components}
    if kinds & {
        ComponentKind.UNQUANTIFIED_EVENT,
        ComponentKind.NON_EQUITY_BONUS,
        ComponentKind.RIGHTS_UNPRICED,
        ComponentKind.RIGHTS_COMPOSITE,
    }:
        action_class = ActionClass.UNQUANTIFIED
    elif ComponentKind.UNRECOGNISED in kinds:
        action_class = ActionClass.UNRECOGNISED
    elif kinds & {
        ComponentKind.SPLIT,
        ComponentKind.CONSOLIDATION,
        ComponentKind.BONUS,
        ComponentKind.RIGHTS,
    }:
        action_class = ActionClass.EQUITY_ADJUSTMENT
    elif _PRICE_WORDS.search(_PURSUANT.sub(" ", text)) and "sale of rights entitlement" not in text:
        action_class = ActionClass.UNRECOGNISED
        notes.append("price-relevant wording that no rule could read")
    elif _CASH.search(text):
        action_class = ActionClass.CASH_DISTRIBUTION
    elif _NO_EFFECT.search(text):
        action_class = ActionClass.NO_PRICE_EFFECT
    else:
        action_class = ActionClass.UNRECOGNISED
    return SubjectInterpretation(
        action_class, tuple(components), tuple(notes), SUBJECT_GRAMMAR_VERSION
    )
