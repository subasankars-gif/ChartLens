/**
 * The real-data unplaced-object count (ADR-0027 §7): runs the chart's own layer adapters
 * over `/chart` responses and counts what they draw and refuse, layer by layer.
 *
 * Input (stdin): one JSON object per line,
 *   {"security_id": …, "chart": <GET /chart?segments=all&sections=…>,
 *    "rows": {"pattern": [<breakout rows>], "level": [<breakout rows>]}}
 * Output (stdout): a JSON summary. Every layer is run with its widest stored selection:
 * every swing method and sensitivity, every stored Fibonacci structure and pattern, every
 * moving average, each oscillator, both breakout datasets.
 *
 * usage: … | pnpm exec tsx scripts/unplaced.mts
 */

import { createInterface } from "node:readline";
import type { ChartResponse } from "../src/lib/api";
import type { BreakoutRow, ChartDocument } from "../src/lib/layers/document";
import { OSCILLATORS, averageNames } from "../src/lib/layers/indicators";
import { frameOf } from "../src/lib/layers/place";
import { ALL_ON, runLayers, type LayerSettings } from "../src/lib/layers/registry";
import { swingChoices } from "../src/lib/layers/structure";
import type { LayerId, LayerResult } from "../src/lib/layers/types";

type Line = { security_id: string; chart: ChartResponse; rows: { pattern: BreakoutRow[]; level: BreakoutRow[] } };

const drawn = new Map<string, number>();
const refused = new Map<string, number>();
const reasons = new Map<string, number>();
const examples: string[] = [];
let securities = 0;
let analysed = 0;

function add(results: readonly LayerResult[], sid: string, only?: ReadonlySet<LayerId>) {
  for (const r of results) {
    if (only && !only.has(r.layer)) continue;
    drawn.set(r.layer, (drawn.get(r.layer) ?? 0) + r.drawn.length);
    refused.set(r.layer, (refused.get(r.layer) ?? 0) + r.refused.length);
    for (const x of r.refused) {
      reasons.set(x.reason, (reasons.get(x.reason) ?? 0) + 1);
      if (examples.length < 25) examples.push(`${sid} ${x.layer} ${x.id}: ${x.reason} (${x.detail})`);
    }
  }
}

const on = (...ids: LayerId[]): ReadonlySet<LayerId> => new Set(ids);

for await (const text of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  if (!text.trim()) continue;
  const line = JSON.parse(text) as Line;
  securities += 1;
  const chart = line.chart;
  if (!chart.analysis) continue;
  analysed += 1;
  const doc = chart.analysis.document as ChartDocument;
  const frame = frameOf(chart.weekly);
  const base: LayerSettings = { ...ALL_ON, averages: doc.indicators ? averageNames(doc.indicators) : [] };
  // Everything except swings and the alternative oscillators/breakouts, once.
  add(
    runLayers(frame, doc, { ...base, enabled: new Set([...ALL_ON.enabled].filter((l) => l !== "swings")) }, line.rows.pattern)
      .results,
    line.security_id,
  );
  for (const oscillator of Object.keys(OSCILLATORS).filter((k) => k !== base.oscillator)) {
    add(
      runLayers(frame, doc, { ...base, oscillator: oscillator as LayerSettings["oscillator"], enabled: on("oscillator") }, null)
        .results,
      line.security_id,
    );
  }
  add(runLayers(frame, doc, { ...base, enabled: on("breakouts") }, line.rows.level).results, line.security_id);
  for (const choice of doc.swings ? swingChoices(doc.swings) : []) {
    add(runLayers(frame, doc, { ...base, swings: choice, enabled: on("swings") }, null).results, line.security_id);
  }
}

const total = (m: Map<string, number>) => [...m.values()].reduce((a, b) => a + b, 0);
process.stdout.write(
  `${JSON.stringify(
    {
      securities,
      analysed,
      drawn: Object.fromEntries(drawn),
      refused: Object.fromEntries(refused),
      drawn_total: total(drawn),
      refused_total: total(refused),
      refusal_reasons: Object.fromEntries(reasons),
      examples,
    },
    null,
    1,
  )}\n`,
);
