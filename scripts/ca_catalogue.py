"""Corporate-action catalogue (Milestone 3, step 1). Runs on a GitHub-hosted runner.

Fetches NSE's full corporate-action history into a local lake using the production
code, then writes catalogue.json: every distinct subject pattern (digits normalised)
with counts and examples, every distinct price-relevant subject verbatim, coverage by
year, and rejected records. Used to design the subject grammar from what NSE actually
writes, not from memory. No bulk data is published — subjects and a few examples only.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from chartlens_core.config import ChartLensSettings
from chartlens_pipeline.corporate_actions import CorporateActionStore
from chartlens_pipeline.http import HttpFetcher
from chartlens_pipeline.providers.nse.corporate_actions import NseCorporateActions
from chartlens_pipeline.storage import LocalObjectStore

PRICE = re.compile(
    r"split|sub-?div|bonus|right|reduc|consolidat|demerg|de-merg|arrangement|amalgam|merger|"
    r"buy ?back|capital|scheme|spin|restructur|face value|conversion|reclassif",
    re.I,
)

lake, out = Path(sys.argv[1]), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
settings = ChartLensSettings()
source = NseCorporateActions(settings.providers.nse, HttpFetcher(settings.http))
store = CorporateActionStore("NSE", source, LocalObjectStore(lake))
start, end = date(2006, 1, 1), date(2026, 12, 31)
fetch = store.fetch(start, end)
rs = store.current_records(start, end)

patterns: Counter[str] = Counter()
examples: dict[str, list[list[str]]] = defaultdict(list)
price_subjects: Counter[str] = Counter()
price_examples: dict[str, list[list[str]]] = defaultdict(list)
no_isin = no_exdate = 0
for rec, _src in rs.records:
    pat = re.sub(r"\d+(\.\d+)?", "#", rec.subject.lower())
    patterns[pat] += 1
    ex = [rec.symbol, rec.isin or "", str(rec.ex_date), str(rec.face_value), rec.series or ""]
    if len(examples[pat]) < 3:
        examples[pat].append(ex)
    if PRICE.search(rec.subject):
        price_subjects[rec.subject] += 1
        if len(price_examples[rec.subject]) < 3:
            price_examples[rec.subject].append(ex)
    no_isin += rec.isin is None
    no_exdate += rec.ex_date is None

catalogue = {
    "fetch": fetch.__dict__,
    "records": len(rs.records),
    "rejected": len(rs.rejected),
    "rejected_sample": rs.rejected[:20],
    "outside_window": rs.outside_window,
    "windows_missing": rs.windows_missing,
    "records_by_ex_year": rs.by_year,
    "records_without_isin": no_isin,
    "records_without_ex_date": no_exdate,
    "series": Counter(r.series for r, _ in rs.records).most_common(),
    "patterns": [
        {"pattern": p, "count": n, "examples": examples[p]} for p, n in patterns.most_common()
    ],
    "price_relevant_subjects": [
        {"subject": s, "count": n, "examples": price_examples[s]}
        for s, n in sorted(price_subjects.items(), key=lambda kv: (-kv[1], kv[0]))
    ],
}
(out / "catalogue.json").write_text(json.dumps(catalogue, indent=1, default=str))
print(
    json.dumps(
        {
            k: catalogue[k]
            for k in (
                "fetch",
                "records",
                "rejected",
                "outside_window",
                "records_by_ex_year",
                "records_without_isin",
            )
        },
        default=str,
    )
)
print("distinct patterns:", len(patterns), " price-relevant subjects:", len(price_subjects))
