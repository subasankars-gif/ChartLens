/**
 * The layer adapters' contract (ADR-0027 §7), over a real `/chart` response (the engine's
 * own output for a synthetic security with a continuity break, a forming week,
 * trendlines, patterns with measured moves, Fibonacci structures; see
 * scripts/layer_fixture.py) and over seeded mutations of it:
 *
 * * every rendered coordinate ∈ the stored (date, value) coordinates of its object;
 * * no interpolation, no extrapolation, no nearest-date matching, no index-based
 *   reconstruction, no cross-segment coordinates;
 * * an object with `known_at` missing or after the snapshot's `as_of` is refused, and
 *   `known_at` never moves geometry;
 * * refused objects are counted, never snapped.
 */

import { readFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";
import { describe, expect, it } from "vitest";
import type { WeeklyResponse } from "../api";
import type { BreakoutRow, ChartDocument } from "./document";
import { breakoutCandidates, divergenceCandidates, evidenceCandidates } from "./events";
import { fibonacciCandidates } from "./fibonacci";
import { indicatorCandidates } from "./indicators";
import { trendlineCandidates, zoneCandidates } from "./levels";
import { patternCandidates } from "./patterns";
import { admit, frameOf, primitiveCoords, primitiveDates } from "./place";
import { ALL_ON, runLayers } from "./registry";
import { structureCandidates, swingCandidates, swingChoices, trendCandidates } from "./structure";
import type { Candidate, LayerId, Primitive } from "./types";

type Fixture = {
  chart: { weekly: WeeklyResponse; analysis: { document: ChartDocument }; meta_version: string; data_as_of: string };
  breakouts: { rows: BreakoutRow[] };
};

function load(): Fixture {
  const raw = gunzipSync(readFileSync(new URL("./__fixtures__/chart-SEC-L.json.gz", import.meta.url)));
  return JSON.parse(raw.toString("utf8")) as Fixture;
}

const FIXTURE = load();
const fresh = (): Fixture => structuredClone(FIXTURE);
const FRAME = frameOf(FIXTURE.chart.weekly);

// --------------------------------------------------------------------------- helpers

/** A seeded PRNG (mulberry32): the mutations are random but reproducible. */
function rng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const ISO = /^\d{4}-\d{2}-\d{2}$/;

/** Every stored date string and every stored number reachable from `values`. */
function stored(...values: unknown[]): { dates: Set<string>; numbers: Set<number> } {
  const dates = new Set<string>();
  const numbers = new Set<number>();
  const walk = (v: unknown) => {
    if (typeof v === "string" && ISO.test(v)) dates.add(v);
    else if (typeof v === "number") numbers.add(v);
    else if (Array.isArray(v)) v.forEach(walk);
    else if (v && typeof v === "object") Object.values(v).forEach(walk);
  };
  values.forEach(walk);
  return { dates, numbers };
}

/** Sibling (date, number) pairs: both stored in the same JSON object. */
function siblingPairs(value: unknown): Set<string> {
  const out = new Set<string>();
  const walk = (v: unknown) => {
    if (Array.isArray(v)) return v.forEach(walk);
    if (!v || typeof v !== "object") return;
    const entries = Object.values(v);
    const ds = entries.filter((x): x is string => typeof x === "string" && ISO.test(x));
    const ns = entries.filter((x): x is number => typeof x === "number");
    for (const d of ds) for (const n of ns) out.add(`${d}|${n}`);
    entries.forEach(walk);
  };
  walk(value);
  return out;
}

type Layer = {
  id: LayerId;
  candidates: (doc: ChartDocument, rows: BreakoutRow[]) => Candidate[];
  /** The stored values an object's coordinates may come from: the object itself plus the
   * section dates its documented span uses. */
  sources: (doc: ChartDocument, rows: BreakoutRow[], id: string) => unknown[];
  /** The stored objects of this layer, mutated in place by the property tests. */
  objects: (doc: ChartDocument, rows: BreakoutRow[]) => Record<string, unknown>[];
};

const find = <T extends Record<string, unknown>>(xs: readonly T[], key: string, id: string): T => {
  const found = xs.find((x) => x[key] === id);
  if (!found) throw new Error(`no stored object ${id}`);
  return found;
};

const LAYERS: Layer[] = [
  {
    id: "swings",
    candidates: (d) => swingChoices(d.swings!).flatMap((c) => swingCandidates(d.swings!, c)),
    sources: (d, _, id) => [find(d.swings!.swings, "swing_id", id)],
    objects: (d) => d.swings!.swings,
  },
  {
    id: "structure",
    candidates: (d) => structureCandidates(d.structure!),
    sources: (d, _, id) =>
      id.endsWith(":LABEL")
        ? [find(d.structure!.labels, "swing_id", id.slice(0, -":LABEL".length))]
        : [find(d.structure!.events, "event_id", id)],
    objects: (d) => [...d.structure!.labels, ...d.structure!.events],
  },
  {
    id: "trend",
    candidates: (d) => trendCandidates(d.structure!),
    sources: (d, _, id) => {
      const h = d.structure!.trend_history;
      const i = h.findIndex((t) => id.endsWith(`:TREND:${t.since}`));
      return [h[i], h[i + 1]?.since, d.structure!.context.as_of];
    },
    objects: (d) => d.structure!.trend_history,
  },
  {
    id: "zones",
    candidates: (d) => zoneCandidates(d.levels!),
    sources: (d, _, id) => [find(d.levels!.zones, "zone_id", id), d.levels!.state_date],
    objects: (d) => d.levels!.zones,
  },
  {
    id: "trendlines",
    candidates: (d) => trendlineCandidates(d.levels!),
    sources: (d, _, id) => [
      find(d.levels!.trendlines, "trendline_id", id),
      d.levels!.active_trendlines.filter((a) => a.trendline_id === id),
      d.levels!.state_date,
    ],
    objects: (d) => d.levels!.trendlines,
  },
  {
    id: "fibonacci",
    candidates: (d) => fibonacciCandidates(d.fibonacci!, d.current!, "all"),
    sources: (d, _, id) => [find(d.fibonacci!.structures, "fib_id", id), d.fibonacci!.state_date],
    objects: (d) => d.fibonacci!.structures,
  },
  {
    id: "divergence",
    candidates: (d) => divergenceCandidates(d.evidence!, ["rsi", "macd"]),
    sources: (d, _, id) => [find(d.evidence!.divergence.divergences, "divergence_id", id)],
    objects: (d) => d.evidence!.divergence.divergences,
  },
  {
    id: "patterns",
    candidates: (d) => patternCandidates(d.patterns!, d.current!, "all"),
    sources: (d, _, id) => [find(d.patterns!.patterns, "pattern_id", id), d.patterns!.context.as_of],
    objects: (d) => d.patterns!.patterns,
  },
  {
    id: "breakouts",
    candidates: (_, rows) => breakoutCandidates(rows),
    sources: (_, rows, id) => [find(rows, "event_id", id)],
    objects: (_, rows) => rows,
  },
  {
    id: "evidence",
    candidates: (d) =>
      evidenceCandidates(d.evidence!, new Set(d.evidence!.candles.events.map((e) => e.event_id))),
    sources: (d, _, id) => [
      [...d.evidence!.volume.events, ...d.evidence!.volatility.events, ...d.evidence!.candles.events].find(
        (e) => e.event_id === id,
      ),
    ],
    objects: (d) => [...d.evidence!.volume.events, ...d.evidence!.volatility.events, ...d.evidence!.candles.events],
  },
  {
    id: "averages",
    candidates: (d) =>
      indicatorCandidates(d.indicators!, d.indicators!.series.map((s) => s.name), "price", () => "average"),
    sources: (d, _, id) => [d.indicators!.bar_dates, find(d.indicators!.series, "name", id.slice("indicators:".length)).data],
    objects: () => [],
  },
];

function placed(layer: Layer, f: Fixture) {
  const doc = f.chart.analysis.document;
  return admit(frameOf(f.chart.weekly), layer.id, layer.candidates(doc, f.breakouts.rows));
}

/** The core property: each drawn coordinate is a stored (date, value) of its object. */
function assertStoredCoordinates(layer: Layer, f: Fixture) {
  const doc = f.chart.analysis.document;
  const result = placed(layer, f);
  for (const d of result.drawn) {
    const sources = layer.sources(doc, f.breakouts.rows, d.id);
    const { dates, numbers } = stored(...sources);
    const pairs = siblingPairs(sources[0]);
    for (const p of d.primitives) {
      for (const date of primitiveDates(p)) expect(dates, `${d.id} date ${date}`).toContain(date);
      for (const c of primitiveCoords(p)) expect(numbers, `${d.id} value ${c.value}`).toContain(c.value);
      if (p.kind === "dot") expect(pairs, `${d.id} dot`).toContain(`${p.at.date}|${p.at.value}`);
    }
  }
  return result;
}

const geometry = (ps: readonly Primitive[]) => ps.filter((p) => p.kind !== "tick");

// --------------------------------------------------------------------------- the fixture

describe("the fixture is the case the checkpoint needs", () => {
  it("has a continuity break, a forming week and rich analysis", () => {
    const w = FIXTURE.chart.weekly;
    expect(new Set(w.bars.map((b) => b.continuity_segment_id)).size).toBe(2);
    expect(w.bars.at(-1)!.is_complete).toBe(false);
    const d = FIXTURE.chart.analysis.document;
    expect(d.levels!.active_trendlines.length).toBeGreaterThan(0);
    expect(d.levels!.trendlines.length).toBeGreaterThan(d.levels!.active_trendlines.length);
    expect(d.patterns!.patterns.some((p) => p.status_history.some((s) => s.measured_move))).toBe(true);
  });
});

// --------------------------------------------------------------------------- properties

describe.each(LAYERS.map((l) => [l.id, l] as const))("%s", (_, layer) => {
  it("draws only stored coordinates, and refuses nothing on real engine output", () => {
    const result = assertStoredCoordinates(layer, fresh());
    expect(result.drawn.length).toBeGreaterThan(0);
    expect(result.refused).toEqual([]);
  });

  it("still draws only stored coordinates when every stored number is random (no interpolation or extrapolation can hide)", () => {
    for (const seed of [1, 2, 3]) {
      const f = fresh();
      const r = rng(seed);
      const randomise = (v: unknown): unknown => {
        if (typeof v === "number") return Math.round((r() * 1000 + 1) * 1e6) / 1e6;
        if (Array.isArray(v)) return v.map(randomise);
        if (v && typeof v === "object") {
          return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, randomise(x)]));
        }
        return v;
      };
      const doc = f.chart.analysis.document as Record<string, unknown>;
      for (const k of Object.keys(doc)) doc[k] = randomise(doc[k]);
      f.breakouts.rows = randomise(f.breakouts.rows) as BreakoutRow[];
      assertStoredCoordinates(layer, f);
    }
  });

  it("ignores stored positions and slopes: scrambling them changes nothing", () => {
    const base = placed(layer, fresh());
    const f = fresh();
    const r = rng(7);
    const scramble = (v: unknown): unknown => {
      if (Array.isArray(v)) return v.map(scramble);
      if (!v || typeof v !== "object") return v;
      return Object.fromEntries(
        Object.entries(v).map(([k, x]) => [
          k,
          /(_index|^anchor_index|slope_per_bar|anchor_value|anchor_1_price|bars_from_previous)$/.test(k) &&
          typeof x === "number"
            ? Math.floor(r() * 10_000)
            : scramble(x),
        ]),
      );
    };
    const doc = f.chart.analysis.document as Record<string, unknown>;
    for (const k of Object.keys(doc)) doc[k] = scramble(doc[k]);
    expect(placed(layer, f)).toEqual(base);
  });

  it("refuses an object whose stored date is off the bars, never snapping it to a neighbour", () => {
    if (layer.id === "averages") return; // covered below, per point
    const f = fresh();
    const doc = f.chart.analysis.document;
    const base = placed(layer, fresh());
    const target = base.drawn[0]!;
    const source = layer.sources(doc, f.breakouts.rows, target.id)[0] as Record<string, unknown>;
    // Move every occurrence of the object's first drawn date to the next calendar day,
    // which is never a bar (bars are labelled by their last session, a weekday).
    const date = primitiveDates(target.primitives[0]!)[0]!;
    const moved = nextDay(date);
    expect(FRAME.barDates.has(moved)).toBe(false);
    replaceDeep(source, date, moved);
    const result = placed(layer, f);
    // Refused whole (a trend span's neighbour shares the moved date, so it is refused too).
    expect(result.refused.length).toBeGreaterThanOrEqual(1);
    for (const x of result.refused) expect(x.reason).toBe("date_not_on_a_bar");
    expect(result.drawn.length + result.refused.length).toBe(base.drawn.length);
    expect(result.drawn.find((x) => x.id === target.id)).toBeUndefined();
    for (const d of result.drawn) for (const p of d.primitives) for (const x of primitiveDates(p)) expect(FRAME.barDates).toContain(x);
  });

  it("refuses an object of another segment", () => {
    const f = fresh();
    const base = placed(layer, fresh());
    const target = base.drawn[0]!;
    const segmentObjects =
      layer.id === "averages" || layer.id === "trend" || layer.id === "structure"
        ? [f.chart.analysis.document[layer.id === "averages" ? "indicators" : "structure"]!.context]
        : [layer.sources(f.chart.analysis.document, f.breakouts.rows, target.id)[0] as Record<string, unknown>];
    for (const o of segmentObjects) (o as Record<string, unknown>).continuity_segment_id = "SEC-L@2010-01-04";
    const result = placed(layer, f);
    expect(result.refused.length).toBeGreaterThanOrEqual(1);
    for (const x of result.refused) expect(x.reason).toBe("other_segment");
    expect(result.drawn.find((x) => x.id === target.id)).toBeUndefined();
  });
});

function nextDay(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + 1);
  return d.toISOString().slice(0, 10);
}

function replaceDeep(o: unknown, from: string, to: string): void {
  if (Array.isArray(o)) {
    o.forEach((x, i) => (x === from ? (o[i] = to) : replaceDeep(x, from, to)));
  } else if (o && typeof o === "object") {
    const rec = o as Record<string, unknown>;
    for (const [k, v] of Object.entries(rec)) {
      if (v === from) rec[k] = to;
      else replaceDeep(v, from, to);
    }
  }
}

// --------------------------------------------------------------------------- time

describe("known_at: visibility, never geometry", () => {
  const withKnown = (knownAt: string | null) =>
    swingCandidates({ ...FIXTURE.chart.analysis.document.swings!, swings: [{ ...firstSwing(), known_at: knownAt }] }, "primary");
  const firstSwing = () => {
    const s = FIXTURE.chart.analysis.document.swings!;
    return s.swings.find(
      (x) => x.method === s.primary_method && x.sensitivity === s.primary_sensitivity && x.continuity_segment_id === FRAME.segmentId,
    )!;
  };

  it("refuses an object known after the snapshot's as_of", () => {
    const r = admit(FRAME, "swings", withKnown("2099-01-01"));
    expect(r.drawn).toEqual([]);
    expect(r.refused[0]!.reason).toBe("known_after_as_of");
  });

  it("refuses an object without a stored known_at", () => {
    expect(admit(FRAME, "swings", withKnown(null)).refused[0]!.reason).toBe("known_at_missing");
  });

  it("draws the geometry at the stored formation coordinates whatever its known_at", () => {
    const s = firstSwing();
    const a = admit(FRAME, "swings", withKnown(s.known_at)).drawn[0]!;
    const b = admit(FRAME, "swings", withKnown(FRAME.asOf)).drawn[0]!;
    expect(b.primitives).toEqual(a.primitives);
    expect(a.primitives[0]).toMatchObject({ kind: "dot", at: { date: s.bar_date, value: s.price } });
  });

  it("refuses a pattern whose status entry is known after as_of, and keeps geometry otherwise", () => {
    const doc = structuredClone(FIXTURE.chart.analysis.document);
    const p = doc.patterns!.patterns[0]!;
    const before = patternCandidates(doc.patterns!, doc.current!, "all").find((c) => c.id === p.pattern_id)!;
    p.known_at = p.end_date <= p.known_at ? p.known_at : p.end_date;
    const after = patternCandidates(doc.patterns!, doc.current!, "all").find((c) => c.id === p.pattern_id)!;
    expect(geometry(after.primitives)).toEqual(geometry(before.primitives));
    p.status_history.at(-1)!.known_at = "2099-01-01";
    expect(admit(FRAME, "patterns", patternCandidates(doc.patterns!, doc.current!, "all")).refused.map((r) => r.id)).toContain(
      p.pattern_id,
    );
  });
});

// --------------------------------------------------------------------------- per layer

describe("trendlines: stored points only (decision 1a)", () => {
  const levels = FIXTURE.chart.analysis.document.levels!;
  const drawn = admit(FRAME, "trendlines", trendlineCandidates(levels)).drawn;

  it("runs an active line through its touches to its stored value at the state date", () => {
    for (const a of levels.active_trendlines) {
      const line = drawn.find((d) => d.id === a.trendline_id)!;
      const path = line.primitives[0]!;
      if (path.kind !== "path") throw new Error("not a path");
      const t = levels.trendlines.find((x) => x.trendline_id === a.trendline_id)!;
      expect(path.points).toEqual([
        ...t.touches.map((x) => ({ date: x.bar_date, value: x.line_value })),
        { date: levels.state_date, value: a.value },
      ]);
    }
  });

  it("ends any other line at its last stored touch", () => {
    const active = new Set(levels.active_trendlines.map((a) => a.trendline_id));
    const others = levels.trendlines.filter((t) => !active.has(t.trendline_id));
    expect(others.length).toBeGreaterThan(0);
    for (const t of others) {
      const path = drawn.find((d) => d.id === t.trendline_id)!.primitives[0]!;
      if (path.kind !== "path") throw new Error("not a path");
      expect(path.points.at(-1)).toEqual({ date: t.touches.at(-1)!.bar_date, value: t.touches.at(-1)!.line_value });
      expect(path.points).toHaveLength(t.touches.length);
    }
  });
});

describe("indicators: the section's own pairing, never bridging a gap", () => {
  const ind = FIXTURE.chart.analysis.document.indicators!;

  it("pairs bar_dates[i] with data[i] and breaks lines at nulls", () => {
    for (const c of indicatorCandidates(ind, ind.series.map((s) => s.name), "price", () => "average")) {
      const series = ind.series.find((s) => `indicators:${s.name}` === c.id)!;
      const byDate = new Map(ind.bar_dates.map((d, i) => [d, series.data[i]]));
      for (const p of c.primitives) {
        if (p.kind !== "path") throw new Error("not a path");
        for (const pt of p.points) expect(byDate.get(pt.date)).toBe(pt.value);
        // Consecutive points are consecutive stored bars: nothing bridges a null.
        for (let i = 1; i < p.points.length; i++) {
          expect(ind.bar_dates.indexOf(p.points[i]!.date)).toBe(ind.bar_dates.indexOf(p.points[i - 1]!.date) + 1);
        }
      }
    }
  });

  it("draws values at provisional (forming-week) bars dashed", () => {
    const rsi = indicatorCandidates(ind, ["rsi"], "oscillator", () => "oscillator")[0]!;
    const last = rsi.primitives.at(-1)!;
    expect(ind.provisional.at(-1)).toBe(true);
    expect(last).toMatchObject({ kind: "path", dashed: true });
  });

  it("proposes nothing for a series with no stored value (warm-up longer than the history)", () => {
    const doc = structuredClone(ind);
    doc.series = [{ ...doc.series[0]!, name: "sma_200", data: doc.series[0]!.data.map(() => null) }];
    expect(indicatorCandidates(doc, ["sma_200"], "price", () => "average")).toEqual([]);
  });

  it("refuses a series with a value at a date that is not a bar", () => {
    const doc = structuredClone(ind);
    const i = doc.bar_dates.length - 3;
    doc.bar_dates[i] = nextDay(doc.bar_dates[i]!);
    const r = admit(FRAME, "averages", indicatorCandidates(doc, ["sma_10"], "price", () => "average"));
    expect(r.refused[0]?.reason).toBe("date_not_on_a_bar");
  });
});

describe("history view: no overlay across a continuity break", () => {
  it("places nothing on an earlier segment's bars", () => {
    const w = FIXTURE.chart.weekly;
    const earlier = w.bars.find((b) => b.continuity_segment_id !== w.current_segment_id)!;
    const s = FIXTURE.chart.analysis.document.swings!;
    const swing = { ...s.swings[0]!, bar_date: earlier.last_session_date, known_at: earlier.last_session_date, continuity_segment_id: w.current_segment_id };
    const r = admit(FRAME, "swings", swingCandidates({ ...s, swings: [swing] }, { method: swing.method, sensitivity: swing.sensitivity }));
    expect(r.refused[0]?.reason).toBe("date_not_on_a_bar");
  });
});

describe("runLayers", () => {
  it("counts zero unplaced objects over the whole real document, with every layer on", () => {
    const doc = FIXTURE.chart.analysis.document;
    const run = runLayers(
      FRAME,
      doc,
      { ...ALL_ON, averages: doc.indicators!.series.map((s) => s.name) },
      FIXTURE.breakouts.rows,
    );
    expect(run.refusedCount).toBe(0);
    expect(run.results.map((r) => r.layer)).toEqual([
      "averages",
      "bollinger",
      "oscillator",
      "swings",
      "structure",
      "trend",
      "zones",
      "trendlines",
      "fibonacci",
      "divergence",
      "patterns",
      "breakouts",
      "evidence",
    ]);
  });

  it("selects the engine's lists by default", () => {
    const doc = FIXTURE.chart.analysis.document;
    const run = runLayers(
      FRAME,
      doc,
      { ...ALL_ON, fibonacci: "current", patterns: "current", enabled: new Set<LayerId>(["fibonacci", "patterns"]) },
      null,
    );
    const ids = (l: LayerId) => run.results.find((r) => r.layer === l)!.drawn.map((d) => d.id);
    expect(new Set(ids("fibonacci"))).toEqual(new Set(doc.current!.fibonacci_ids));
    expect(new Set(ids("patterns"))).toEqual(new Set(doc.current!.included_pattern_ids));
  });
});
