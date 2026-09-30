"""Bhavcopy parser: real NSE samples (both formats, three eras) plus synthetic edge cases."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from nse_fakes import fixture_csv, zipped

from chartlens_pipeline.daily import QuarantineReason, SourceParseError
from chartlens_pipeline.providers.nse.bhavcopy import LEGACY, UDIFF, detect_format, parse_bhavcopy

U = frozenset({"EQ", "BE"})
LEGACY_HEADER = "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,"
UDIFF_HEADER = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,"
    "StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,"
    "SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,"
    "Rsvd1,Rsvd2,Rsvd3,Rsvd4"
)
D = date(2015, 1, 5)


def legacy(*rows: str) -> bytes:
    return "\n".join([LEGACY_HEADER, *rows]).encode()


def row(
    symbol: str = "ABC",
    series: str = "EQ",
    o: str = "10",
    h: str = "12",
    lo: str = "9",
    c: str = "11",
    qty: str = "100",
    day: str = "05-JAN-2015",
    isin: str = "INE002A01018",
) -> str:
    return f"{symbol},{series},{o},{h},{lo},{c},11,10,{qty},1100.5,{day},7,{isin},"


# ----------------------------------------------------------------------------- real samples


@pytest.mark.parametrize(
    ("name", "day", "fmt", "has_isin"),
    [
        ("legacy_2006-01-02", date(2006, 1, 2), "legacy", False),
        ("legacy_2011-06-01", date(2011, 6, 1), "legacy", False),
        ("legacy_2015-01-05", date(2015, 1, 5), "legacy", True),
        ("legacy_2024-01-20", date(2024, 1, 20), "legacy", True),
        ("legacy_2024-07-05", date(2024, 7, 5), "legacy", True),
        ("udiff_2024-01-19", date(2024, 1, 19), "udiff", True),
        ("udiff_2024-07-05", date(2024, 7, 5), "udiff", True),
        ("udiff_2025-02-01", date(2025, 2, 1), "udiff", True),
        ("udiff_2026-09-29", date(2026, 9, 29), "udiff", True),
    ],
)
def test_real_nse_samples_parse_cleanly(name: str, day: date, fmt: str, has_isin: bool) -> None:
    parsed = parse_bhavcopy(fixture_csv(name), day, U)
    assert parsed.source_format == fmt
    assert parsed.rows, "every sample has in-universe rows"
    assert parsed.quarantined == []
    assert parsed.warnings == []
    assert all(r.series in U and r.trading_date == day for r in parsed.rows)
    assert all((r.isin is not None) == has_isin for r in parsed.rows)


def test_legacy_and_udiff_files_for_the_same_day_agree() -> None:
    """NSE published both formats for 2024-07-05; every shared row must be identical."""
    day = date(2024, 7, 5)
    legacy_rows = {
        (r.symbol, r.series): r
        for r in parse_bhavcopy(fixture_csv("legacy_2024-07-05"), day, U).rows
    }
    udiff_rows = {
        (r.symbol, r.series): r
        for r in parse_bhavcopy(fixture_csv("udiff_2024-07-05"), day, U).rows
    }
    shared = set(legacy_rows) & set(udiff_rows)
    assert ("RELIANCE", "EQ") in shared
    fields = (
        "isin",
        "open",
        "high",
        "low",
        "close",
        "prev_close",
        "volume",
        "traded_value",
        "trades",
    )
    for key in shared:
        assert [getattr(legacy_rows[key], f) for f in fields] == [
            getattr(udiff_rows[key], f) for f in fields
        ]


def test_reliance_2024_07_05_values_are_exact() -> None:
    parsed = parse_bhavcopy(fixture_csv("udiff_2024-07-05"), date(2024, 7, 5), U)
    rel = next(r for r in parsed.rows if r.symbol == "RELIANCE")
    assert (rel.isin, rel.open, rel.high, rel.low, rel.close) == (
        "INE002A01018",
        Decimal("3107.65"),
        Decimal("3197.00"),
        Decimal("3096.00"),
        Decimal("3177.25"),
    )
    assert (rel.volume, rel.trades, rel.name) == (6134855, 261494, "RELIANCE INDUSTRIES LTD")


def test_legacy_unpadded_day_and_missing_isin_columns() -> None:
    parsed = parse_bhavcopy(fixture_csv("legacy_2006-01-02"), date(2006, 1, 2), U)  # "2-JAN-2006"
    rel = next(r for r in parsed.rows if r.symbol == "RELIANCE")
    assert rel.isin is None and rel.trades is None
    assert rel.close == Decimal("897.85")


def test_udiff_header_variant_with_rsvd01_and_trailing_column() -> None:
    parsed = parse_bhavcopy(fixture_csv("udiff_2024-01-19"), date(2024, 1, 19), U)
    assert parsed.source_format == "udiff" and parsed.warnings == []


def test_zipped_and_plain_bytes_parse_identically() -> None:
    plain = fixture_csv("udiff_2026-09-29")
    a = parse_bhavcopy(plain, date(2026, 9, 29), U)
    b = parse_bhavcopy(zipped("BhavCopy.csv", plain), date(2026, 9, 29), U)
    assert a.rows == b.rows and b.member_name == "BhavCopy.csv"


def test_parser_is_deterministic() -> None:
    content = fixture_csv("legacy_2015-01-05")
    assert parse_bhavcopy(content, D, U) == parse_bhavcopy(content, D, U)


# ----------------------------------------------------------------------------- detection


def test_format_is_detected_from_header_not_filename() -> None:
    """Legacy content inside a zip member named like a UDiFF file is still legacy."""
    content = zipped("BhavCopy_NSE_CM_0_0_0_20150105_F_0000.csv", legacy(row()))
    assert parse_bhavcopy(content, D, U).source_format == "legacy"


def test_detect_format_requires_all_required_columns() -> None:
    assert detect_format([c.upper() for c in LEGACY_HEADER.split(",") if c]) is LEGACY
    assert detect_format([c.upper() for c in UDIFF_HEADER.split(",")]) is UDIFF
    with pytest.raises(SourceParseError, match=r"UNKNOWN_FORMAT.*missing"):
        detect_format(["SYMBOL", "SERIES", "OPEN"])


# ----------------------------------------------------------------------------- file-level failures


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"", "EMPTY_SOURCE"),
        (LEGACY_HEADER.encode(), "EMPTY_SOURCE"),
        (b"PK\x03\x04garbage", "BAD_ZIP"),
        (b"A,B,C\n1,2,3\n", "UNKNOWN_FORMAT"),
        (b"SYMBOL,SYMBOL,SERIES\n", "DUPLICATE_COLUMNS"),
    ],
)
def test_unusable_files_raise_with_a_reason(content: bytes, reason: str) -> None:
    with pytest.raises(SourceParseError) as err:
        parse_bhavcopy(content, D, U)
    assert err.value.reason == reason


def test_missing_required_column_is_a_file_error() -> None:
    header = LEGACY_HEADER.replace("CLOSE,", "")
    with pytest.raises(SourceParseError, match=r"UNKNOWN_FORMAT.*CLOSE"):
        parse_bhavcopy(f"{header}\n{row()}".encode(), D, U)


def test_zip_with_two_csv_members_is_rejected() -> None:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.csv", legacy(row()))
        zf.writestr("b.csv", legacy(row()))
    with pytest.raises(SourceParseError, match="ZIP_LAYOUT"):
        parse_bhavcopy(buf.getvalue(), D, U)


def test_file_for_another_date_is_rejected() -> None:
    with pytest.raises(SourceParseError, match="SOURCE_DATE_MISMATCH"):
        parse_bhavcopy(legacy(row(day="06-JAN-2015")), D, U)


def test_unexpected_columns_are_reported_not_fatal() -> None:
    content = f"{LEGACY_HEADER}NEWCOL\n{row()}X".encode()
    parsed = parse_bhavcopy(content, D, U)
    assert len(parsed.rows) == 1
    assert parsed.warnings == ["UNEXPECTED_COLUMNS: ['NEWCOL']"]


def test_non_utf8_bytes_fall_back_with_a_warning() -> None:
    content = legacy(row()).replace(b"ABC", b"AB\xe9")  # cp1252 'é'
    parsed = parse_bhavcopy(content, D, U)
    assert parsed.rows[0].symbol == "ABÉ"
    assert parsed.warnings and parsed.warnings[0].startswith("ENCODING_FALLBACK")


def test_utf8_bom_is_accepted() -> None:
    parsed = parse_bhavcopy(b"\xef\xbb\xbf" + legacy(row()), D, U)
    assert len(parsed.rows) == 1 and parsed.warnings == []


# ----------------------------------------------------------------------------- row-level quarantine


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        (row(h="8"), QuarantineReason.INVALID_OHLC),  # high < low
        (row(h="10.5", c="11"), QuarantineReason.INVALID_OHLC),  # high < close
        (row(lo="10.5"), QuarantineReason.INVALID_OHLC),  # low > open
        (row(o="0"), QuarantineReason.NON_POSITIVE_PRICE),
        (row(o="-1"), QuarantineReason.NON_POSITIVE_PRICE),
        (row(o="1O"), QuarantineReason.INVALID_NUMBER),
        (row(o=""), QuarantineReason.MISSING_FIELD),
        (row(o="10.12345"), QuarantineReason.PRICE_PRECISION),
        (row(qty="10.5"), QuarantineReason.INVALID_NUMBER),
        (row(qty="-5"), QuarantineReason.NEGATIVE_VOLUME),
        (row(day="31-FEB-2015"), QuarantineReason.INVALID_DATE),
        (row(isin="INE002A01019"), QuarantineReason.INVALID_ISIN),
        (row(symbol=""), QuarantineReason.MISSING_FIELD),
        ("ABC,EQ,10,12", QuarantineReason.MALFORMED_ROW),
        (row(series=""), QuarantineReason.MISSING_FIELD),
    ],
)
def test_invalid_rows_are_quarantined_not_dropped(line: str, reason: QuarantineReason) -> None:
    parsed = parse_bhavcopy(legacy(row(symbol="GOOD", isin="INE144J01027"), line), D, U)
    assert [r.symbol for r in parsed.rows] == ["GOOD"]
    assert [q.reason for q in parsed.quarantined] == [reason]
    assert parsed.quarantined[0].raw.startswith(line.split(",")[0])


def test_row_dated_differently_from_file_is_quarantined() -> None:
    parsed = parse_bhavcopy(
        legacy(row(), row(symbol="XYZ", day="06-JAN-2015", isin="INE144J01027")), D, U
    )
    assert [q.reason for q in parsed.quarantined] == [QuarantineReason.DATE_MISMATCH]


def test_duplicate_symbol_series_rows_are_all_quarantined() -> None:
    parsed = parse_bhavcopy(
        legacy(row(), row(c="11.5"), row(symbol="OTHER", isin="INE144J01027")), D, U
    )
    assert [r.symbol for r in parsed.rows] == ["OTHER"]
    assert [q.reason for q in parsed.quarantined] == [QuarantineReason.DUPLICATE_ROW] * 2


def test_out_of_universe_series_are_counted_not_quarantined() -> None:
    parsed = parse_bhavcopy(
        legacy(row(), row(symbol="BOND", series="N1"), row(symbol="SME", series="SM")), D, U
    )
    assert len(parsed.rows) == 1 and parsed.quarantined == []
    assert parsed.out_of_scope == {"N1": 1, "SM": 1}


def test_udiff_rows_outside_capital_market_segment_are_skipped() -> None:
    line = (
        "2015-01-05,2015-01-05,{seg},NSE,STK,1,INE002A01018,ABC,EQ,,,,,ABC LTD,10,12,9,11,11,10,,11,,,100,"
        "1100,7,F1,1,,,,,"
    )
    content = "\n".join([UDIFF_HEADER, line.format(seg="CM"), line.format(seg="FO")]).encode()
    parsed = parse_bhavcopy(content, D, U)
    assert len(parsed.rows) == 1 and parsed.out_of_scope == {"(other segment)": 1}


def test_blank_lines_are_ignored() -> None:
    parsed = parse_bhavcopy(legacy(row(), "", " , ,"), D, U)
    assert parsed.rows_read == 1 and len(parsed.rows) == 1


# ----------------------------------------------------------------------------- properties

prices = st.decimals(min_value=Decimal("0.05"), max_value=Decimal("99999"), places=2)


@settings(max_examples=150, deadline=None)
@given(a=prices, b=prices, c=prices, d=prices, qty=st.integers(0, 10**9))
def test_every_accepted_row_satisfies_ohlc_invariants(
    a: Decimal, b: Decimal, c: Decimal, d: Decimal, qty: int
) -> None:
    """Arbitrary price quadruples: a row is either accepted and valid, or quarantined."""
    parsed = parse_bhavcopy(
        legacy(row(o=str(a), h=str(b), lo=str(c), c=str(d), qty=str(qty))), D, U
    )
    for r in parsed.rows:
        assert r.high >= max(r.open, r.close)
        assert r.low <= min(r.open, r.close)
        assert r.high >= r.low
        assert min(r.open, r.high, r.low, r.close) > 0 and r.volume >= 0
    assert len(parsed.rows) + len(parsed.quarantined) == 1
    valid = b >= max(a, d) and c <= min(a, d) and b >= c
    assert bool(parsed.rows) == valid
