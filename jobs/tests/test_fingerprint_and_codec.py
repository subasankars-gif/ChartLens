"""The frozen dependency fingerprint (ADR-0025 §3) and the event codec on the engine's
own rows (ADR-0025 §5)."""

from __future__ import annotations

from datetime import date
from typing import Any, get_args, get_origin

import pyarrow as pa
import pytest
from analysis_chain import context, random_bars
from chartlens_jobs.fingerprint import (
    REUSE_KEY_VERSION,
    RUNTIME_PACKAGES,
    DependencyFingerprint,
    Formats,
    runtime_versions,
)

from chartlens_core.canonical import content_hash, dataset_content_hash
from chartlens_core.config import AnalysisConfig
from chartlens_engine.analysis import AnalysisInputs, analysis_version, analyze_security, serialize
from chartlens_engine.bar_evidence import BarVolumeEvidence
from chartlens_engine.breakouts import LevelBreakoutEvent, PatternBreakoutEvent
from chartlens_engine.breakouts.model import BreakoutFollowUp
from chartlens_engine.interfaces import AnalysisContext
from chartlens_pipeline.analysis_store import EVENT_SCHEMAS, decode_events, encode_events

INPUTS = AnalysisInputs(
    exchange="NSE",
    weekly_schema_version="1",
    weekly_builder_version="1",
    usable_from=date(2006, 1, 6),
)


def fingerprint(**changes: Any) -> DependencyFingerprint:
    bars = random_bars(50, 0)
    base: dict[str, Any] = {
        "context": context(bars),
        "bars_sha256": "a" * 64,
        "inputs": INPUTS,
        "analysis_version": analysis_version(AnalysisConfig()),
        "runtime": runtime_versions(),
    }
    return DependencyFingerprint(**{**base, **changes})


# ----------------------------------------------------------------------------- fingerprint


def test_the_fingerprint_holds_exactly_the_frozen_fields() -> None:
    """ADR-0025 §3.2. Adding or removing a field is a contract change (and a
    ``reuse_key_version`` bump), never an accident."""
    assert set(DependencyFingerprint.model_fields) == {
        "reuse_key_version",
        "context",
        "bars_sha256",
        "inputs",
        "analysis_version",
        "formats",
        "runtime",
    }
    assert DependencyFingerprint.model_fields["context"].annotation is AnalysisContext
    assert DependencyFingerprint.model_fields["inputs"].annotation is AnalysisInputs
    assert fingerprint().reuse_key_version == REUSE_KEY_VERSION
    assert (
        set(runtime_versions())
        == {"python", *RUNTIME_PACKAGES}
        == {
            "python",
            "numpy",
            "pandas",
            "pydantic",
        }
    )


def test_operational_metadata_cannot_enter_the_key() -> None:
    """No run id, timestamp, snapshot id, physical hash, path, worker or order: the
    models forbid any field they do not declare."""
    names = set(DependencyFingerprint.model_fields) | set(AnalysisInputs.model_fields)
    names |= set(AnalysisContext.model_fields)
    for banned in ("run_id", "timestamp", "meta_version", "weekly_file_sha256", "path", "worker"):
        assert not any(banned in n for n in names), banned
    with pytest.raises(ValueError):
        fingerprint(weekly_file_sha256="b" * 64)


def _perturbations() -> list[tuple[str, dict[str, Any]]]:
    base = fingerprint()
    ctx, inp = base.context, base.inputs
    out: list[tuple[str, dict[str, Any]]] = [
        ("bars_sha256", {"bars_sha256": "b" * 64}),
        ("analysis_version", {"analysis_version": "analysis-000000000000"}),
        ("reuse_key_version", {"reuse_key_version": "0"}),
        ("formats", {"formats": Formats(document_schema_version="0")}),
        ("runtime.numpy", {"runtime": {**base.runtime, "numpy": "0.0.0"}}),
        ("runtime.python", {"runtime": {**base.runtime, "python": "3.11"}}),
    ]
    for name, value in (
        ("security_id", "OTHER"),
        ("as_of", date(1999, 1, 1)),
        ("methodology_hash", "other"),
        ("continuity_segment_id", "OTHER@2006-01-06"),
    ):
        out.append((f"context.{name}", {"context": ctx.model_copy(update={name: value})}))
    for name, value in (
        ("exchange", "BSE"),
        ("weekly_schema_version", "9"),
        ("weekly_builder_version", "9"),
        ("usable_from", date(2007, 1, 5)),
    ):
        out.append((f"inputs.{name}", {"inputs": inp.model_copy(update={name: value})}))
    return out


@pytest.mark.parametrize(("name", "change"), _perturbations(), ids=lambda x: str(x)[:30])
def test_every_dependency_moves_the_key(name: str, change: dict[str, Any]) -> None:
    assert fingerprint(**change).reuse_key() != fingerprint().reuse_key(), name


def test_every_context_and_input_field_is_perturbed() -> None:
    """Whole-model dumps: a field added to either model is covered by the key, and must
    be added to the perturbation list above."""
    covered = {n for n, _ in _perturbations()}
    for field in AnalysisContext.model_fields:
        if field != "timeframe":  # a single value (WEEKLY) today
            assert f"context.{field}" in covered, field
    for field in AnalysisInputs.model_fields:
        assert f"inputs.{field}" in covered, field


def test_the_key_is_a_pure_function_of_the_fingerprint() -> None:
    assert fingerprint().reuse_key() == fingerprint().reuse_key()
    assert fingerprint().reuse_key() == content_hash(fingerprint().model_dump(mode="json"))


# ----------------------------------------------------------------------------- the codec


def _arrow_names(t: pa.DataType) -> set[str]:
    return {t.field(i).name for i in range(t.num_fields)}


def _model_of(annotation: Any) -> Any:
    for arg in (annotation, *get_args(annotation)):
        if isinstance(arg, type) and hasattr(arg, "model_fields"):
            return arg
        if get_origin(arg) is list:
            return _model_of(get_args(arg)[0])
    return None


@pytest.mark.parametrize(
    ("dataset", "model"),
    [("pattern_breakouts", PatternBreakoutEvent), ("level_breakouts", LevelBreakoutEvent)],
)
def test_each_schema_matches_the_engine_model(dataset: str, model: Any) -> None:
    """A field added to an event model without the codec fails here, not in production."""
    schema = EVENT_SCHEMAS[dataset]  # type: ignore[index]
    assert schema.names == list(model.model_fields)
    struct = pa.struct(list(schema))
    for name, sub in (("bar_volume", BarVolumeEvidence), ("history", BreakoutFollowUp)):
        t = struct.field(name).type
        t = t.value_type if pa.types.is_list(t) else t
        assert _arrow_names(t) == set(sub.model_fields), name
        assert _model_of(model.model_fields[name].annotation) is sub


@pytest.mark.parametrize("seed", [0, 6])
def test_the_engines_events_round_trip_exactly(seed: int) -> None:
    bars = random_bars(900, seed)
    inputs = INPUTS
    stored = serialize(analyze_security(bars, context(bars), AnalysisConfig(), inputs))
    for name, ds in stored.events.items():
        data = encode_events(name, ds.rows, ds.metadata)
        dataset, rows, meta = decode_events(data)
        assert dataset == name and meta == ds.metadata
        assert rows == ds.rows
        assert dataset_content_hash(meta, rows) == ds.content_sha256
    assert stored.events["level_breakouts"].row_count > 0
