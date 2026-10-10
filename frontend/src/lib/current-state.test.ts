import { readFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";
import { describe, expect, it } from "vitest";
import { breakoutsSince, claimGroup, groupInOrder, structureChangesSince } from "./current-state";
import { ageInDays } from "./format";
import type { BreakoutRow, ChartDocument } from "./layers/document";

type Fixture = { chart: { analysis: { document: ChartDocument } }; breakouts: { rows: BreakoutRow[] } };
const FIXTURE = JSON.parse(
  gunzipSync(readFileSync(new URL("./layers/__fixtures__/chart-SEC-L.json.gz", import.meta.url))).toString("utf8"),
) as Fixture;
const DOC = FIXTURE.chart.analysis.document;

// Every claim type the engine writes (chartlens_engine.explain; ADR-0028 §10 templates).
const CLAIM_TYPES = [
  "DATA_CONTEXT",
  "FORMING_WEEK",
  "TREND_STATE",
  "ZONE",
  "ZONE_ROLE_REVERSED",
  "ACTIVE_TRENDLINE",
  "FIBONACCI",
  "FIBONACCI_LEVEL",
  "PATTERN",
  "PATTERN_CONFIRMATION",
  "PATTERN_INVALIDATION",
  "PATTERN_MEASURED_MOVE",
  "PATTERN_FIT",
  "PATTERN_TAG",
];

describe("claims are grouped, never reordered (ADR-0028: engine order only)", () => {
  it("every claim type the engine writes has a group", () => {
    for (const t of CLAIM_TYPES) expect(claimGroup(t), t).not.toBe("other");
  });

  it("grouping keeps the stored order exactly, even if a category came back later", () => {
    const claims = ["DATA_CONTEXT", "TREND_STATE", "ZONE", "ZONE", "ACTIVE_TRENDLINE", "PATTERN", "PATTERN_FIT", "ZONE"].map(
      (claim_type, i) => ({ claim_type, claim_id: `c${i}` }),
    );
    const groups = groupInOrder(claims);
    expect(groups.flatMap((g) => g.claims)).toEqual(claims); // same objects, same order
    expect(groups.map((g) => g.group)).toEqual(["context", "trend", "levels", "patterns", "levels"]);
  });
});

describe("changes since the current trend state: selection by the engine's own date", () => {
  const since = DOC.current!.trend_since!;

  it("returns the stored structure events on or after trend_since, as stored", () => {
    const got = structureChangesSince(DOC, since);
    const stored = DOC.structure!.events;
    expect(got).toEqual(stored.filter((e) => e.bar_date >= since));
    for (const e of got) expect(stored).toContain(e); // the stored objects themselves, nothing built
    expect(got.map((e) => e.bar_date)).toEqual([...got.map((e) => e.bar_date)].sort());
  });

  it("is empty, not invented, when there is no trend state or no structure section", () => {
    expect(structureChangesSince(DOC, null)).toEqual([]);
    expect(structureChangesSince({ ...DOC, structure: undefined }, since)).toEqual([]);
  });

  it("filters breakout rows of one dataset without merging or reordering", () => {
    const rows = FIXTURE.breakouts.rows;
    const got = breakoutsSince(rows, since);
    expect(got).toEqual(rows.filter((r) => r.bar_date >= since));
    for (const r of got) expect(rows).toContain(r);
    expect(breakoutsSince(rows, null)).toEqual([]);
  });
});

describe("snapshot age (a date shown, not analysis)", () => {
  it("counts whole days and never goes negative", () => {
    expect(ageInDays("2026-10-09", "2026-10-10")).toBe(1);
    expect(ageInDays("2026-10-01", "2026-10-10")).toBe(9);
    expect(ageInDays("2026-10-10", "2026-10-09")).toBe(0);
  });
});
