"""The logical identity of an analysis's input bars (ADR-0025 §1).

> ``bars_sha256`` is the SHA-256 of the canonical serialized representation of the exact
> bar sequence supplied to the analytical engine for that security and segment.

The orchestrator computes it from the frame it was given; it never accepts it from the
caller. The job layer calls the same function to build its reuse key, and checks the two
agree. The encoding (``BARS_ENCODING_VERSION``): canonical JSON of
``{"encoding", "columns", "rows"}``; every column in the frame's order, every row in the
frame's order; dates as ISO text (a timestamp with a time of day is refused); floats,
integers, booleans, strings and nulls as canonical JSON writes them; a missing value in
a text column is null (pandas may mark it None, NaN or NA); NaN in a numeric column is
refused.
"""

from __future__ import annotations

import pandas as pd

from chartlens_core.bars import column
from chartlens_core.canonical import CanonicalError, content_hash

BARS_ENCODING_VERSION = "bars-1"


def _column(series: pd.Series) -> list[object]:
    if pd.api.types.is_datetime64_any_dtype(series.dtype):
        out: list[object] = []
        for ts in pd.DatetimeIndex(series):
            if ts != ts.normalize():
                raise CanonicalError(f"{series.name}: {ts} has a time of day")
            out.append(ts.date().isoformat())
        return out
    if pd.api.types.is_float_dtype(series.dtype):
        return list(series.tolist())  # a NaN price or volume is refused by the encoder
    # Text and object columns: a missing value is null, whichever marker pandas used
    # (None, NaN in a string column, pd.NA).
    return [None if _missing(v) else v for v in series.tolist()]


def _missing(value: object) -> bool:
    if value is None or value is pd.NA:
        return True
    return isinstance(value, float) and value != value


def bars_content_hash(bars: pd.DataFrame) -> str:
    columns = [str(c) for c in bars.columns]
    values = [_column(column(bars, c)) for c in columns]
    rows = [list(row) for row in zip(*values, strict=True)] if values else []
    return content_hash({"encoding": BARS_ENCODING_VERSION, "columns": columns, "rows": rows})
