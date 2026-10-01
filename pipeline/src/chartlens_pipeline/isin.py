"""ISIN handling (ISO 6166), exchange-neutral."""

from __future__ import annotations

import re

_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def normalize_isin(text: str | None) -> str | None:
    """Upper-cased, stripped ISIN, or None for a blank value."""
    if text is None:
        return None
    stripped = text.strip().upper()
    return stripped or None


def isin_check_digit(body: str) -> int:
    """Check digit for the first 11 characters of an ISIN (letters A=10 … Z=35, then Luhn)."""
    digits = "".join(str(int(ch, 36)) for ch in body)
    total = 0
    # Luhn from the right: the rightmost payload digit is doubled.
    for position, ch in enumerate(reversed(digits)):
        d = int(ch)
        if position % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10


def is_valid_isin(isin: str) -> bool:
    return bool(_ISIN.match(isin)) and isin_check_digit(isin[:11]) == int(isin[11])
