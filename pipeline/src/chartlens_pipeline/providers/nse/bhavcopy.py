"""NSE capital-market bhavcopy parser (ADR-0008).

Two formats exist in NSE's archive (verified by the NSE probe, 2026-09-30):

``legacy``  ``cm{DD}{MON}{YYYY}bhav.csv`` — published until 2024-07-05.
            Columns SYMBOL, SERIES, OPEN … TIMESTAMP; TOTALTRADES and ISIN appear
            only from ~2011 (absent in 2006, 2009 and June-2011 files).
``udiff``   ``BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv`` — published from at least
            2024-01-19. Header seen in two variants (``Rsvd01…`` with a trailing empty
            column in Jan 2024; ``Rsvd1…`` later).

Structure: **detect → map → common row pipeline**. Detection looks at the header
columns, never the filename. Each format is only a :class:`FormatSpec` (which
column holds which field, date format, row filters); every row of every format
then goes through the same parsing and validation code, so both formats produce
exactly the same canonical output.

Deterministic: the result is a pure function of the file bytes, the expected date
and the universe series.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Final

from chartlens_pipeline.daily import (
    FieldError,
    NormalizedRow,
    ParsedDaily,
    QuarantinedRow,
    QuarantineReason,
    SourceParseError,
    parse_date,
    parse_price,
    parse_quantity,
    structural_problem,
)
from chartlens_pipeline.isin import is_valid_isin, normalize_isin

PARSER_VERSION: Final = "nse_bhavcopy_v2"
"""v2 (2026-09-30): legacy dates may also use a two-digit year (``13-Jul-20``), seen in
the 2020-07-13 file during the 20-year verification run. v1 quarantined that day."""


@dataclass(frozen=True)
class FormatSpec:
    name: str
    columns: Mapping[str, str]
    """canonical field → source column (normalised to upper case)."""
    optional: frozenset[str]
    """canonical fields that may be absent from the header."""
    date_formats: tuple[str, ...]
    """Accepted date formats, tried in order. The file-date check still requires every
    accepted row to carry exactly the requested session date."""
    row_filters: Mapping[str, frozenset[str]] = field(default_factory=dict)
    """source column → accepted values; other rows are not part of this dataset."""
    ignored: frozenset[str] = frozenset()
    """Known columns ChartLens does not use. Anything else is reported as unexpected."""

    def required_columns(self) -> set[str]:
        return {col for f, col in self.columns.items() if f not in self.optional}


LEGACY: Final = FormatSpec(
    name="legacy",
    columns={
        "symbol": "SYMBOL",
        "series": "SERIES",
        "open": "OPEN",
        "high": "HIGH",
        "low": "LOW",
        "close": "CLOSE",
        "prev_close": "PREVCLOSE",
        "volume": "TOTTRDQTY",
        "traded_value": "TOTTRDVAL",
        "date": "TIMESTAMP",
        "trades": "TOTALTRADES",
        "isin": "ISIN",
    },
    optional=frozenset({"trades", "isin"}),
    date_formats=("%d-%b-%Y", "%d-%b-%y"),
    ignored=frozenset({"LAST"}),
)

UDIFF: Final = FormatSpec(
    name="udiff",
    columns={
        "symbol": "TCKRSYMB",
        "series": "SCTYSRS",
        "open": "OPNPRIC",
        "high": "HGHPRIC",
        "low": "LWPRIC",
        "close": "CLSPRIC",
        "prev_close": "PRVSCLSGPRIC",
        "volume": "TTLTRADGVOL",
        "traded_value": "TTLTRFVAL",
        "date": "TRADDT",
        "trades": "TTLNBOFTXSEXCTD",
        "isin": "ISIN",
        "name": "FININSTRMNM",
    },
    optional=frozenset({"trades", "name"}),
    date_formats=("%Y-%m-%d",),
    row_filters={"SGMT": frozenset({"CM"})},
    ignored=frozenset(
        {
            "BIZDT",
            "SRC",
            "FININSTRMTP",
            "FININSTRMID",
            "XPRYDT",
            "FININSTRMACTLXPRYDT",
            "STRKPRIC",
            "OPTNTP",
            "LASTPRIC",
            "UNDRLYGPRIC",
            "STTLMPRIC",
            "OPNINTRST",
            "CHNGINOPNINTRST",
            "SSNID",
            "NEWBRDLOTQTY",
            "RMKS",
            "RSVD1",
            "RSVD2",
            "RSVD3",
            "RSVD4",
            "RSVD01",
            "RSVD02",
            "RSVD03",
            "RSVD04",
        }
    ),
)

FORMATS: Final = (UDIFF, LEGACY)


# ----------------------------------------------------------------------------- file level


def _unzip(content: bytes) -> tuple[bytes, str | None]:
    if content[:2] != b"PK":
        return content, None  # already plain CSV (tests, or an unzipped source)
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            members = [n for n in zf.namelist() if not n.endswith("/")]
            csvs = [n for n in members if n.lower().endswith(".csv")]
            if len(csvs) != 1:
                raise SourceParseError(
                    "ZIP_LAYOUT", f"expected exactly one CSV member, found {members}"
                )
            return zf.read(csvs[0]), csvs[0]
    except zipfile.BadZipFile as exc:
        raise SourceParseError("BAD_ZIP", str(exc)) from None


def _decode(data: bytes, warnings: list[str]) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        warnings.append(
            f"ENCODING_FALLBACK: not valid UTF-8 ({exc.reason} at byte {exc.start}); read as cp1252"
        )
        return data.decode("cp1252", errors="replace")


def _normalise_header(raw: list[str]) -> list[str]:
    header = [h.strip().upper() for h in raw]
    while header and header[-1] == "":
        header.pop()  # NSE files end each line with a trailing comma
    return header


def detect_format(header: list[str]) -> FormatSpec:
    columns = set(header)
    for spec in FORMATS:
        if spec.required_columns() <= columns:
            return spec
    closest = min(FORMATS, key=lambda s: len(s.required_columns() - columns))
    missing = sorted(closest.required_columns() - columns)
    raise SourceParseError(
        "UNKNOWN_FORMAT",
        f"header matches no known bhavcopy format (closest: {closest.name}, missing {missing}); "
        f"header={header}",
    )


# ----------------------------------------------------------------------------- row level


def _row_text(cells: list[str]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="").writerow(cells)
    return buffer.getvalue()


def _parse_row(spec: FormatSpec, values: Mapping[str, str], row_number: int) -> NormalizedRow:
    def get(field_name: str) -> str | None:
        col = spec.columns.get(field_name)
        return values.get(col) if col is not None else None

    def required(field_name: str) -> str:
        value = get(field_name)
        if value is None or not value.strip():
            raise FieldError(QuarantineReason.MISSING_FIELD, f"{field_name} is empty")
        return value

    symbol = required("symbol").strip().upper()
    series = required("series").strip().upper()
    isin_text = normalize_isin(get("isin"))
    if isin_text is not None and not is_valid_isin(isin_text):
        raise FieldError(
            QuarantineReason.INVALID_ISIN, f"ISIN {isin_text!r} fails format/check digit"
        )
    prev_close = get("prev_close")
    traded_value = get("traded_value")
    trades = get("trades")
    name = get("name")
    return NormalizedRow(
        row_number=row_number,
        trading_date=parse_date(required("date"), spec.date_formats, "date"),
        symbol=symbol,
        series=series,
        isin=isin_text,
        name=name.strip() if name and name.strip() else None,
        open=parse_price(required("open"), "open"),
        high=parse_price(required("high"), "high"),
        low=parse_price(required("low"), "low"),
        close=parse_price(required("close"), "close"),
        prev_close=parse_price(prev_close, "prev_close")
        if prev_close and prev_close.strip()
        else None,
        volume=parse_quantity(required("volume"), "volume"),
        traded_value=parse_price(traded_value, "traded_value")
        if traded_value and traded_value.strip()
        else None,
        trades=parse_quantity(trades, "trades") if trades and trades.strip() else None,
    )


def parse_bhavcopy(
    content: bytes, expected_date: date, universe_series: frozenset[str]
) -> ParsedDaily:
    """Parse one NSE bhavcopy file (zipped or plain CSV) into the canonical row model."""
    if not content:
        raise SourceParseError("EMPTY_SOURCE", "file is empty")
    warnings: list[str] = []
    csv_bytes, member = _unzip(content)
    text = _decode(csv_bytes, warnings)
    reader = csv.reader(io.StringIO(text))
    try:
        raw_header = next(reader)
    except StopIteration:
        raise SourceParseError("EMPTY_SOURCE", "no header line") from None
    header = _normalise_header(raw_header)
    duplicates = sorted(h for h, n in Counter(header).items() if n > 1 and h)
    if duplicates:
        raise SourceParseError("DUPLICATE_COLUMNS", f"columns repeated in header: {duplicates}")
    spec = detect_format(header)
    known = set(spec.columns.values()) | set(spec.row_filters) | spec.ignored
    unexpected = [h for h in header if h and h not in known]
    if unexpected:
        warnings.append(f"UNEXPECTED_COLUMNS: {unexpected}")

    rows: list[NormalizedRow] = []
    quarantined: list[QuarantinedRow] = []
    out_of_scope: Counter[str] = Counter()
    rows_read = 0
    width = len(header)
    raw_lines: dict[int, str] = {}

    for line_no, cells in enumerate(reader, start=2):
        if not any(c.strip() for c in cells):
            continue
        rows_read += 1
        raw = _row_text(cells)
        raw_lines[line_no] = raw
        trimmed = list(cells)
        while len(trimmed) > width and trimmed[-1].strip() == "":
            trimmed.pop()
        if len(trimmed) != width:
            quarantined.append(
                QuarantinedRow(
                    line_no,
                    QuarantineReason.MALFORMED_ROW,
                    f"{len(trimmed)} fields, header has {width}",
                    None,
                    None,
                    None,
                    raw,
                )
            )
            continue
        values = dict(zip(header, (c.strip() for c in trimmed), strict=True))
        if any(
            values.get(col, "").upper() not in allowed for col, allowed in spec.row_filters.items()
        ):
            out_of_scope["(other segment)"] += 1
            continue
        series = values.get(spec.columns["series"], "").strip().upper()
        symbol = values.get(spec.columns["symbol"], "").strip().upper() or None
        isin = normalize_isin(values.get(spec.columns.get("isin", "")))
        if not series:
            quarantined.append(
                QuarantinedRow(
                    line_no,
                    QuarantineReason.MISSING_FIELD,
                    "series is empty",
                    symbol,
                    None,
                    isin,
                    raw,
                )
            )
            continue
        if series not in universe_series:
            out_of_scope[series] += 1
            continue
        try:
            row = _parse_row(spec, values, line_no)
        except FieldError as err:
            quarantined.append(
                QuarantinedRow(line_no, err.reason, err.detail, symbol, series, isin, raw)
            )
            continue
        problem = structural_problem(row)
        if problem is not None:
            quarantined.append(
                QuarantinedRow(line_no, problem[0], problem[1], symbol, series, isin, raw)
            )
            continue
        rows.append(row)

    if rows_read == 0:
        raise SourceParseError("EMPTY_SOURCE", "header present but no data rows")

    # The file must describe the requested session; rows dated otherwise are quarantined.
    dates = Counter(r.trading_date for r in rows)
    if dates and dates.most_common(1)[0][0] != expected_date:
        raise SourceParseError(
            "SOURCE_DATE_MISMATCH",
            f"file is for {dates.most_common(1)[0][0]}, expected {expected_date}",
        )
    kept: list[NormalizedRow] = []
    for r in rows:
        if r.trading_date != expected_date:
            quarantined.append(
                QuarantinedRow(
                    r.row_number,
                    QuarantineReason.DATE_MISMATCH,
                    f"row dated {r.trading_date}, file is {expected_date}",
                    r.symbol,
                    r.series,
                    r.isin,
                    raw_lines[r.row_number],
                )
            )
        else:
            kept.append(r)

    # The same (symbol, series) twice in one file cannot be resolved by choosing one.
    counts = Counter((r.symbol, r.series) for r in kept)
    final: list[NormalizedRow] = []
    for r in kept:
        if counts[(r.symbol, r.series)] > 1:
            quarantined.append(
                QuarantinedRow(
                    r.row_number,
                    QuarantineReason.DUPLICATE_ROW,
                    f"{r.symbol}/{r.series} appears {counts[(r.symbol, r.series)]} times",
                    r.symbol,
                    r.series,
                    r.isin,
                    raw_lines[r.row_number],
                )
            )
        else:
            final.append(r)

    return ParsedDaily(
        source_format=spec.name,
        parser_version=PARSER_VERSION,
        trading_date=expected_date,
        member_name=member,
        rows_read=rows_read,
        rows=final,
        quarantined=sorted(quarantined, key=lambda q: q.row_number),
        out_of_scope=dict(sorted(out_of_scope.items())),
        warnings=warnings,
        raw_lines={r.row_number: raw_lines[r.row_number] for r in final},
    )
