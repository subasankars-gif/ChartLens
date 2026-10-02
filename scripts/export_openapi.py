"""Write the API's OpenAPI document to docs/api/openapi.json (the documented contract).

CI regenerates it and fails if the committed copy is stale; the frontend's TypeScript
types are generated from it (``pnpm gen:api``), so the two cannot drift apart.

usage: python scripts/export_openapi.py [OUT]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from chartlens_api.main import create_app
from chartlens_core.config import ChartLensSettings

out = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/api/openapi.json")
spec = create_app(ChartLensSettings.model_construct()).openapi()
out.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
print(f"wrote {out}")
