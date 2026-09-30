"""The canonical daily-bar model (exchange-neutral) — ADR-0010.

Everything a provider's parser produces is expressed in these types, so every
exchange yields the same contract and downstream code never sees a source format.

Numeric representation
----------------------
Prices are exact decimals, never floats: ``Decimal`` in Python and
``decimal128(18, 4)`` in Parquet (4 decimal places: NSE quotes to 2, and 4 leaves
room for sub-cent quotes on other exchanges). Values with more than 4 decimal places
are quarantined rather than rounded. Traded value is ``decimal128(24, 4)``. Volumes
and trade counts are ``int64``. Conversion to float happens only at the engine
boundary (Milestone 4), where the bar-frame contract requires float64.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq

DAILY_BAR_SCHEMA_VERSION: Final = 1
QUARANTINE_SCHEMA_VERSION: Final = 1
PRICE_SCALE: Final = 4
PRICE_TYPE: Final = pa.decimal128(18, PRICE_SCALE)
VALUE_TYPE: Final = pa.decimal128(24, PRICE_SCALE)

_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


class QuarantineReason(StrEnum):
    # structural (parser)
    MALFORMED_ROW = "MALFORMED_ROW"
    MISSING_FIELD = "MISSING_FIELD"
    INVALID_NUMBER = "INVALID_NUMBER"
    PRICE_PRECISION = "PRICE_PRECISION"
    INVALID_DATE = "INVALID_DATE"
    DATE_MISMATCH = "DATE_MISMATCH"
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    NEGATIVE_VOLUME = "NEGATIVE_VOLUME"
    INVALID_OHLC = "INVALID_OHLC"
    INVALID_ISIN = "INVALID_ISIN"
    DUPLICATE_ROW = "DUPLICATE_ROW"
    # identity (resolver)
    UNRESOLVED_IDENTITY = "UNRESOLVED_IDENTITY"
    AMBIGUOUS_IDENTITY = "AMBIGUOUS_IDENTITY"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    DUPLICATE_SECURITY_DATE = "DUPLICATE_SECURITY_DATE"


IDENTITY_REASONS: Final = frozenset(
    {
        QuarantineReason.UNRESOLVED_IDENTITY,
        QuarantineReason.AMBIGUOUS_IDENTITY,
        QuarantineReason.IDENTITY_CONFLICT,
    }
)
"""Reasons that may clear on reprocessing once more identity evidence exists."""


class FieldError(ValueError):
    def __init__(self, reason: QuarantineReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class NormalizedRow:
    """One in-universe row from a source file, parsed and structurally valid."""

    row_number: int
    trading_date: date
    symbol: str
    series: str
    isin: str | None
    name: str | None
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    prev_close: Decimal | None
    volume: int
    traded_value: Decimal | None
    trades: int | None


@dataclass(frozen=True, slots=True)
class QuarantinedRow:
    row_number: int
    reason: QuarantineReason
    detail: str
    symbol: str | None
    series: str | None
    isin: str | None
    raw: str
    """The original line, verbatim, for diagnosis."""


@dataclass
class ParsedDaily:
    """Result of parsing one daily source file. Pure function of the file's bytes."""

    source_format: str
    parser_version: str
    trading_date: date
    member_name: str | None
    rows_read: int
    rows: list[NormalizedRow]
    quarantined: list[QuarantinedRow]
    out_of_scope: dict[str, int] = field(default_factory=dict)
    """Rows skipped because their series is outside the configured universe, by series."""
    warnings: list[str] = field(default_factory=list)


class SourceParseError(ValueError):
    """The whole file is unusable (not just some rows)."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


# ----------------------------------------------------------------------------- field parsing


def parse_price(text: str, field_name: str) -> Decimal:
    value = parse_decimal(text, field_name)
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > PRICE_SCALE:
        raise FieldError(
            QuarantineReason.PRICE_PRECISION,
            f"{field_name}={text!r} has more than {PRICE_SCALE} decimal places",
        )
    return value


def parse_decimal(text: str, field_name: str) -> Decimal:
    stripped = text.strip()
    if not stripped:
        raise FieldError(QuarantineReason.MISSING_FIELD, f"{field_name} is empty")
    if not _NUMBER.match(stripped):
        raise FieldError(QuarantineReason.INVALID_NUMBER, f"{field_name}={text!r} is not a number")
    try:
        return Decimal(stripped)
    except InvalidOperation:  # pragma: no cover — regex already guarantees validity
        raise FieldError(QuarantineReason.INVALID_NUMBER, f"{field_name}={text!r}") from None


def parse_quantity(text: str, field_name: str) -> int:
    value = parse_decimal(text, field_name)
    if value != value.to_integral_value():
        raise FieldError(QuarantineReason.INVALID_NUMBER, f"{field_name}={text!r} is not a whole number")
    return int(value)


def parse_date(text: str, fmt: str, field_name: str) -> date:
    stripped = text.strip()
    if not stripped:
        raise FieldError(QuarantineReason.MISSING_FIELD, f"{field_name} is empty")
    try:
        return datetime.strptime(stripped, fmt).date()  # noqa: DTZ007 — a calendar date, no time
    except ValueError:
        raise FieldError(QuarantineReason.INVALID_DATE, f"{field_name}={text!r} is not a valid date") from None


def structural_problem(row: NormalizedRow) -> tuple[QuarantineReason, str] | None:
    """Basic validity (spec §23). Adjustment-aware checks belong to Milestone 3."""
    prices = {"open": row.open, "high": row.high, "low": row.low, "close": row.close}
    non_positive = [k for k, v in prices.items() if v <= 0]
    if non_positive:
        return QuarantineReason.NON_POSITIVE_PRICE, f"non-positive {', '.join(non_positive)}"
    if row.volume < 0:
        return QuarantineReason.NEGATIVE_VOLUME, f"volume={row.volume}"
    if row.high < row.low:
        return QuarantineReason.INVALID_OHLC, f"high {row.high} < low {row.low}"
    if row.high < max(row.open, row.close):
        return QuarantineReason.INVALID_OHLC, f"high {row.high} < max(open, close)"
    if row.low > min(row.open, row.close):
        return QuarantineReason.INVALID_OHLC, f"low {row.low} > min(open, close)"
    return None


# ----------------------------------------------------------------------------- Parquet schemas


def daily_bar_schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field("exchange", pa.string(), nullable=False),
            pa.field("security_id", pa.string(), nullable=False),
            pa.field("trading_date", pa.date32(), nullable=False),
            pa.field("symbol", pa.string(), nullable=False),
            pa.field("series", pa.string(), nullable=False),
            pa.field("isin", pa.string()),
            pa.field("open", PRICE_TYPE, nullable=False),
            pa.field("high", PRICE_TYPE, nullable=False),
            pa.field("low", PRICE_TYPE, nullable=False),
            pa.field("close", PRICE_TYPE, nullable=False),
            pa.field("prev_close", PRICE_TYPE),
            pa.field("volume", pa.int64(), nullable=False),
            pa.field("traded_value", VALUE_TYPE),
            pa.field("trades", pa.int64()),
            pa.field("source_id", pa.string(), nullable=False),
            pa.field("source_file_hash", pa.string(), nullable=False),
            pa.field("source_file_date", pa.date32(), nullable=False),
            pa.field("parser_version", pa.string(), nullable=False),
            pa.field("ingested_at", pa.timestamp("us", tz="UTC"), nullable=False),
        ],
        metadata={
            b"chartlens.dataset": b"daily_bar",
            b"chartlens.schema_version": str(DAILY_BAR_SCHEMA_VERSION).encode(),
            b"chartlens.price_scale": str(PRICE_SCALE).encode(),
        },
    )


def quarantine_schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field("exchange", pa.string(), nullable=False),
            pa.field("trading_date", pa.date32(), nullable=False),
            pa.field("source_id", pa.string(), nullable=False),
            pa.field("source_file_hash", pa.string(), nullable=False),
            pa.field("parser_version", pa.string(), nullable=False),
            pa.field("row_number", pa.int64(), nullable=False),
            pa.field("reason", pa.string(), nullable=False),
            pa.field("detail", pa.string(), nullable=False),
            pa.field("symbol", pa.string()),
            pa.field("series", pa.string()),
            pa.field("isin", pa.string()),
            pa.field("raw", pa.string(), nullable=False),
            pa.field("quarantined_at", pa.timestamp("us", tz="UTC"), nullable=False),
        ],
        metadata={
            b"chartlens.dataset": b"daily_quarantine",
            b"chartlens.schema_version": str(QUARANTINE_SCHEMA_VERSION).encode(),
        },
    )


def to_parquet_bytes(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    pq.write_table(table, sink, compression="zstd")
    return sink.getvalue()


def from_parquet_bytes(data: bytes, schema: pa.Schema) -> pa.Table:
    """Read a table and verify it matches ``schema`` exactly (types, names, version)."""
    table = pq.read_table(io.BytesIO(data))
    if not table.schema.equals(schema, check_metadata=False):
        raise ValueError(f"schema mismatch:\n{table.schema}\n!=\n{schema}")
    expected = (schema.metadata or {}).get(b"chartlens.schema_version")
    found = (table.schema.metadata or {}).get(b"chartlens.schema_version")
    if expected != found:
        raise ValueError(f"schema version {found!r} != expected {expected!r}")
    return table
