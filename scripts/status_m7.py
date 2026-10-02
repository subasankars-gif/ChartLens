"""Read-only check of the first tracked production refresh (throwaway probe)."""

import json
import os

from chartlens_pipeline.runs import FirestoreRunStore
from chartlens_pipeline.storage import DataLakeLayout, GcsObjectStore

store = GcsObjectStore(os.environ["BUCKET"])
runs = FirestoreRunStore(os.environ["PROJECT"])
out: dict[str, object] = {}
recent = runs.list(5)
out["runs"] = [r.model_dump(mode="json") for r in recent]
last = runs.last_successful()
out["last_successful_run"] = last.run_id if last else None
out["active"] = (a.run_id if (a := runs.active()) else None)
out["snapshots"] = [s.model_dump(mode="json") for s in runs.list_snapshots(5)]
manifest = json.loads(store.get(DataLakeLayout.serving_manifest_key("NSE")))
out["serving"] = {k: manifest.get(k) for k in ("meta_version", "schema_version", "as_of", "run_id", "generated_at", "counts")}
weekly_keys = store.list(DataLakeLayout.serving_weekly_prefix("NSE"))
referenced = {DataLakeLayout.serving_weekly_key("NSE", d) for d in manifest["weekly_files"].values()}
out["weekly_copies"] = len(weekly_keys)
out["weekly_referenced"] = len(referenced)
out["missing_copies"] = len(referenced - set(weekly_keys))
prefix = DataLakeLayout.serving_prefix("NSE")
out["version_dirs"] = sorted({k[len(prefix):].split("/", 1)[0] for k in store.list(prefix + "v=")})
print(json.dumps(out, indent=1, default=str))
