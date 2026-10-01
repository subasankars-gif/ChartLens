"""Identity rebuild: new identity inputs take effect only through a full re-resolution of
stored history; retired ids are aliased, never reused (ADR-0013). Modelled on 3i Infotech."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from nse_fakes import FakeNse, calendar_for, fake_provider, legacy_zip
from test_adjust import bhav

from chartlens_core.config import ChartLensSettings, NseProviderConfig, ProvidersConfig
from chartlens_core.domain import DataQualityStatus as Q
from chartlens_pipeline.adjust import AdjustmentService, CorporateActionOverrides
from chartlens_pipeline.data_quality import DataQualityService
from chartlens_pipeline.identity import IdentityOverrides
from chartlens_pipeline.identity_rebuild import IdentityRebuildService
from chartlens_pipeline.ingest import IdentityInputsChanged, IngestionService
from chartlens_pipeline.storage import DataLakeLayout, LocalObjectStore

OLD, NEW = "INE748C01020", "INE748C01038"
SESSIONS = [
    d for d in (date(2021, 8, 2) + timedelta(days=i) for i in range(100)) if d.weekday() < 5
]
BEFORE, AFTER = SESSIONS[:20], SESSIONS[40:]  # 20 sessions without a trade in between
LINK = IdentityOverrides(
    link_isin={NEW: OLD},
    link_breaks_continuity={NEW: "reviewed: capital reduction"},
    fingerprint="reviewed-3i",
)
SETTINGS = ChartLensSettings.model_construct(
    providers=ProvidersConfig(nse=NseProviderConfig(corporate_actions_first_month=date(2021, 8, 1)))
)


def today() -> date:
    return date(2021, 12, 31)


@pytest.fixture
def lake(tmp_path: Path) -> tuple[LocalObjectStore, FakeNse]:
    fake = FakeNse()
    for day in SESSIONS:
        rows = [("INFY", "INE009A01021", "1500")]
        if day in BEFORE:
            rows.append(("3IINFOTECH", OLD, "8"))
        if day in AFTER:
            rows.append(("3IINFOLTD", NEW, "60"))
        fake.serve(*legacy_zip(day, bhav(day, rows)))
    store = LocalObjectStore(tmp_path / "lake")
    provider = fake_provider(fake, calendar_for([2021]), SETTINGS)
    IngestionService(
        SETTINGS, provider, store, overrides=IdentityOverrides(), today=today
    ).backfill(SESSIONS[0], SESSIONS[-1])
    return store, fake


def sids_by_symbol(store: LocalObjectStore) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for key in store.list(DataLakeLayout.curated_daily_prefix("NSE")):
        for r in pq.read_table(pa.BufferReader(store.get(key))).to_pylist():
            out.setdefault(r["symbol"], set()).add(r["security_id"])
    return out


def test_new_identity_inputs_take_effect_only_through_a_rebuild(
    lake: tuple[LocalObjectStore, FakeNse],
) -> None:
    store, fake = lake
    provider = fake_provider(fake, calendar_for([2021]), SETTINGS)
    before = sids_by_symbol(store)
    assert before["3IINFOTECH"] != before["3IINFOLTD"]  # rules alone keep them apart

    with pytest.raises(IdentityInputsChanged):
        IngestionService(SETTINGS, provider, store, overrides=LINK, today=today).backfill(
            SESSIONS[-1], SESSIONS[-1]
        )

    report = IdentityRebuildService(SETTINGS, provider, store, overrides=LINK, today=today).run()
    assert report.status == "COMPLETE" and report.errors == []
    assert (report.securities_before, report.securities_after) == (3, 2)
    after = sids_by_symbol(store)
    assert after["3IINFOTECH"] == after["3IINFOLTD"]  # one security now
    assert after["INFY"] == before["INFY"]  # untouched securities keep their id
    (alias,) = report.aliases
    survivor = next(iter(after["3IINFOLTD"]))
    assert alias.security_id == survivor and alias.reason == "ISIN"
    assert alias.retired_security_id in before["3IINFOTECH"] | before["3IINFOLTD"]
    assert report.merged_into == {survivor: [alias.retired_security_id]}

    table = pq.read_table(pa.BufferReader(store.get(DataLakeLayout.security_aliases_key("NSE"))))
    assert table.to_pylist()[0]["security_id"] == survivor
    record = json.loads(store.get(DataLakeLayout.identity_rebuild_key("NSE", report.rebuild_id)))
    assert record["status"] == "COMPLETE" and record["new_inputs"]["overrides"] == "reviewed-3i"

    # Ingestion now runs with the new inputs, and refuses the old ones.
    IngestionService(SETTINGS, provider, store, overrides=LINK, today=today).backfill(
        SESSIONS[-1], SESSIONS[-1]
    )
    with pytest.raises(IdentityInputsChanged):
        IngestionService(
            SETTINGS, provider, store, overrides=IdentityOverrides(), today=today
        ).backfill(SESSIONS[-1], SESSIONS[-1])

    # The reviewed link joins identity, not prices: usable_from = first session of the new ISIN.
    adjusted = AdjustmentService(
        SETTINGS,
        provider,
        store,
        overrides=CorporateActionOverrides(),
        identity_overrides=LINK,
        today=today,
    ).run()
    # The +650% gap at the link is classified, not "unexplained", and never adjusted.
    assert adjusted.report.at_reviewed_identity_breaks == 1 and adjusted.report.unexplained == 0
    dq = DataQualityService(SETTINGS, provider, store, identity_overrides=LINK).run()
    status = next(s for s in dq.statuses if s["security_id"] == survivor)
    assert status["first_date"] == BEFORE[0] and status["usable_from"] == AFTER[0]
    assert status["status"] in (Q.USABLE, Q.USABLE_WITH_WARNINGS)
    codes = [f.code for f in dq.findings if f.security_id == survivor]
    assert "REVIEWED_LINK_PRICE_BREAK" in codes and "UNEXPLAINED_MOVE" not in codes
    assert "ISIN_CHANGE" in codes and "SYMBOL_CHANGE" in codes
    assert not any(
        f.detail.split(" → ")[0] == f.detail.split(" → ")[-1]
        for f in dq.findings
        if f.code in ("ISIN_CHANGE", "SYMBOL_CHANGE")
    )


def test_an_interrupted_rebuild_blocks_ingestion(lake: tuple[LocalObjectStore, FakeNse]) -> None:
    store, fake = lake
    provider = fake_provider(fake, calendar_for([2021]), SETTINGS)
    key = DataLakeLayout.identity_state_key("NSE")
    state = json.loads(store.get(key))
    store.put(key, json.dumps({**state, "rebuild_in_progress": "x"}).encode())
    with pytest.raises(IdentityInputsChanged, match="did not complete"):
        IngestionService(
            SETTINGS, provider, store, overrides=IdentityOverrides(), today=today
        ).backfill(SESSIONS[-1], SESSIONS[-1])
    # Re-running the rebuild recovers.
    IdentityRebuildService(
        SETTINGS, provider, store, overrides=IdentityOverrides(), today=today
    ).run()
    IngestionService(
        SETTINGS, provider, store, overrides=IdentityOverrides(), today=today
    ).backfill(SESSIONS[-1], SESSIONS[-1])


def test_rebuild_with_unchanged_inputs_is_a_no_op_on_identity(
    lake: tuple[LocalObjectStore, FakeNse],
) -> None:
    store, fake = lake
    provider = fake_provider(fake, calendar_for([2021]), SETTINGS)
    before = sids_by_symbol(store)
    report = IdentityRebuildService(
        SETTINGS, provider, store, overrides=IdentityOverrides(), today=today
    ).run()
    assert report.aliases == [] and sids_by_symbol(store) == before
