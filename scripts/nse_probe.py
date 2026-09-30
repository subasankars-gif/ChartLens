"""NSE connectivity and format probe.

Run on a GitHub-hosted runner (``.github/workflows/nse-probe.yml``) to answer two
questions without guessing:

1. Can a hosted runner reach NSE's archives at all (ADR-0007 risk)?
2. What do the real files look like across the archive's history (headers,
   member names, series present, ISIN coverage)?

Only **trimmed samples** (header + a few rows) and a JSON report are written. Full
NSE files are never uploaded or committed: the repository is public and NSE data
is not ours to redistribute.

Standard library only, so it runs before any project dependency is installed.
"""

from __future__ import annotations

import csv
import hashlib
import http.cookiejar
import io
import json
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
SAMPLE_SYMBOLS = {"RELIANCE", "INFY", "TCS", "HDFCBANK"}


def legacy_urls(d: date) -> list[str]:
    mon = MONTHS[d.month - 1]
    path = f"content/historical/EQUITIES/{d.year}/{mon}/cm{d.day:02d}{mon}{d.year}bhav.csv.zip"
    return [f"https://nsearchives.nseindia.com/{path}", f"https://archives.nseindia.com/{path}"]


def udiff_url(d: date) -> str:
    return (
        "https://nsearchives.nseindia.com/content/cm/"
        f"BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
    )


LEGACY_DATES = [
    date(2006, 1, 2),
    date(2009, 3, 2),
    date(2011, 6, 1),
    date(2015, 1, 5),
    date(2020, 3, 2),
    date(2024, 1, 20),  # Saturday special session
    date(2024, 1, 26),  # Republic Day holiday
    date(2024, 7, 5),
    date(2024, 7, 8),
]
UDIFF_DATES = [
    date(2020, 3, 2),
    date(2024, 1, 19),
    date(2024, 7, 5),
    date(2024, 7, 8),
    date(2024, 11, 1),  # Muhurat
    date(2025, 2, 1),  # Saturday Budget session
    date(2025, 2, 3),
    date(2026, 9, 29),
]
OTHER = {
    "equity_list": "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
    "symbol_changes": "https://nsearchives.nseindia.com/content/equities/symbolchange.csv",
}


def fetch(opener: urllib.request.OpenerDirector, url: str) -> dict[str, object]:
    started = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with opener.open(req, timeout=30) as resp:
            body = resp.read()
            status, ctype = resp.status, resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as err:
        body, status, ctype = err.read(), err.code, err.headers.get("Content-Type", "")
    except Exception as err:  # the probe records every failure mode
        elapsed = time.monotonic() - started
        return {"url": url, "error": f"{type(err).__name__}: {err}", "seconds": elapsed}
    return {
        "url": url,
        "status": status,
        "content_type": ctype,
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "head": body[:120].decode("latin-1"),
        "seconds": round(time.monotonic() - started, 2),
        "_body": body,
    }


def summarise_csv(text: str) -> tuple[dict[str, object], list[list[str]]]:
    rows = list(csv.reader(io.StringIO(text)))
    header = rows[0] if rows else []
    body = rows[1:]
    upper = [h.strip().upper() for h in header]
    series_col = next((i for i, h in enumerate(upper) if h in ("SERIES", "SCTYSRS")), None)
    isin_col = next((i for i, h in enumerate(upper) if h == "ISIN"), None)
    sym_col = next((i for i, h in enumerate(upper) if h in ("SYMBOL", "TCKRSYMB")), None)
    series_counts: dict[str, int] = {}
    missing_isin = 0
    for r in body:
        if series_col is not None and series_col < len(r):
            series_counts[r[series_col].strip()] = series_counts.get(r[series_col].strip(), 0) + 1
        if isin_col is None or isin_col >= len(r) or not r[isin_col].strip():
            missing_isin += 1
    keep = body[:25]
    if sym_col is not None:
        keep += [r for r in body if sym_col < len(r) and r[sym_col].strip() in SAMPLE_SYMBOLS]
    if series_col is not None:
        keep += [r for r in body if series_col < len(r) and r[series_col].strip() == "BE"][:5]
    top = dict(sorted(series_counts.items(), key=lambda kv: -kv[1])[:15])
    return (
        {"header": header, "rows": len(body), "series_top": top, "rows_missing_isin": missing_isin},
        [header, *keep],
    )


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    samples = out / "samples"
    samples.mkdir(exist_ok=True)
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    report: dict[str, object] = {"runner_checks": [], "files": []}

    targets: list[tuple[str, str]] = []
    for d in LEGACY_DATES:
        targets += [(f"legacy_{d}_{i}", u) for i, u in enumerate(legacy_urls(d))]
    targets += [(f"udiff_{d}", udiff_url(d)) for d in UDIFF_DATES]
    targets += list(OTHER.items())

    for name, url in targets:
        res = fetch(opener, url)
        body = res.pop("_body", b"")
        entry: dict[str, object] = {"name": name, **res}
        if isinstance(body, bytes) and body[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(body)) as zf:
                entry["zip_members"] = zf.namelist()
                raw = zf.read(zf.namelist()[0])
            text = raw.decode("utf-8", errors="replace")
        elif isinstance(body, bytes) and name in OTHER and res.get("status") == 200:
            text = body.decode("utf-8", errors="replace")
        else:
            text = ""
        if text:
            summary, keep = summarise_csv(text)
            entry.update(summary)
            with (samples / f"{name}.csv").open("w", newline="") as fh:
                csv.writer(fh, lineterminator="\n").writerows(keep)
        report["files"].append(entry)  # type: ignore[union-attr]
        print(f"{name:32} {entry.get('status', entry.get('error'))} {entry.get('bytes', '')}")
        time.sleep(1.0)

    # Holiday API needs a session cookie from the main site first.
    home = fetch(opener, "https://www.nseindia.com/")
    home.pop("_body", None)
    hol = fetch(opener, "https://www.nseindia.com/api/holiday-master?type=trading")
    hol_body = hol.pop("_body", b"")
    report["runner_checks"] = [home, hol]
    if hol.get("status") == 200 and isinstance(hol_body, bytes):
        (out / "holiday_master.json").write_bytes(hol_body)

    (out / "report.json").write_text(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "probe-out"))
