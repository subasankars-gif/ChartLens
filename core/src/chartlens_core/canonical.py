"""Canonical serialization (ADR-0024 §4, amendment C; in core per ADR-0025).

Content addresses are computed from these bytes, so the encoding is fixed and versioned
(``CANONICAL_SERIALIZATION_VERSION``):

- **UTF-8 JSON, no insignificant whitespace.**
- **Mappings** (order-irrelevant by definition) are written with their keys sorted by
  code point. Keys are strings (Pydantic's JSON mode writes them so; a test holds every
  document to it).
- **Sequences keep the order they were given.** The serializer never sorts a list: the
  layer that produced it owns its order (amendment C).
- **Numbers:** an integer is written in decimal; a float as its shortest round-trip
  representation (``float.__repr__``), so ``float(text) == value`` exactly. Negative
  zero keeps its sign: it is a distinct value, and the encoding is lossless. NaN and
  infinities are refused (a layer represents a missing value as null).
- **Null** is ``null``; booleans are ``true`` / ``false``; strings use JSON escapes,
  with non-ASCII characters kept as UTF-8.
- **Anything else is refused** (a date must already be ISO text, as Pydantic's JSON mode
  writes it), so no value reaches the bytes through an implicit conversion.

Display formatting (ADR-0023 K7: a bar's exact decimal text, 4-decimal derived values)
belongs to serving, not to the stored form: the stored document stays lossless.

The encoder is the standard library's, pinned by these options; the tests compare it
with an independent reference encoder written from the rules above.

It lives in ``core`` because the engine (documents), the pipeline (validating stored
artifacts without importing the engine) and the job layer (reuse keys) all address
content the same way (ADR-0025). It is generic: it knows JSON values and hashes, and no
analytical or business rule.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

CANONICAL_SERIALIZATION_VERSION = "1"


class CanonicalError(TypeError):
    """A value the canonical encoding does not represent."""


def _refuse(value: object) -> object:
    raise CanonicalError(f"{type(value).__name__} is not canonical JSON")


def canonical_json(value: object) -> bytes:
    """The canonical bytes of a JSON-ready value."""
    try:
        text = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
            default=_refuse,
        )
    except ValueError as exc:  # NaN or infinity
        raise CanonicalError(str(exc)) from exc
    return text.encode("utf-8")


def content_hash(value: object) -> str:
    """SHA-256 (hex) of the canonical bytes."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


DATASET_CONTENT_KEY = "chartlens.content_sha256"
"""The metadata key that carries a dataset's content hash; it is never part of the
hashed metadata itself."""


def dataset_content_hash(metadata: Mapping[str, str], rows: Sequence[object]) -> str:
    """A dataset's logical identity: its identifying metadata and its rows (ADR-0025 §5).
    Two datasets with the same rows but different owners (say, two securities with no
    events) are different datasets, so their addresses differ."""
    identity = {k: v for k, v in metadata.items() if k != DATASET_CONTENT_KEY}
    return content_hash({"metadata": identity, "rows": list(rows)})
