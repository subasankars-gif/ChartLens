/**
 * The layer toggles (ADR-0023, ADR-0027 §1): what each needs from the document and how
 * it runs. The default chart is candles and volume only; every layer starts off.
 * Selections are among authoritative choices: engine-given lists (`current.*`), stored
 * attributes (swing method and sensitivity), or stored series names.
 */

import type { BreakoutRow, ChartDocument } from "./document";
import { breakoutLayer, divergenceLayer, evidenceLayer } from "./events";
import { fibonacciLayer, type Selection } from "./fibonacci";
import { BOLLINGER, OSCILLATORS, indicatorLayer } from "./indicators";
import { trendlineLayer, zoneLayer } from "./levels";
import { citedEvidence, patternLayer } from "./patterns";
import { structureLayer, swingLayer, trendLayer, type SwingSelection } from "./structure";
import type { Frame, LayerId, LayerResult } from "./types";

export type Section = keyof ChartDocument;

export type LayerSpec = { id: LayerId; label: string; sections: readonly Section[] };

export const LAYERS: readonly LayerSpec[] = [
  { id: "averages", label: "Moving averages", sections: ["indicators"] },
  { id: "bollinger", label: "Bollinger bands", sections: ["indicators"] },
  { id: "oscillator", label: "Oscillator", sections: ["indicators"] },
  { id: "swings", label: "Swings", sections: ["swings"] },
  { id: "structure", label: "Market structure", sections: ["structure"] },
  { id: "trend", label: "Trend state", sections: ["structure"] },
  { id: "zones", label: "Support and resistance", sections: ["levels"] },
  { id: "trendlines", label: "Trendlines", sections: ["levels"] },
  { id: "fibonacci", label: "Fibonacci", sections: ["fibonacci", "current"] },
  { id: "divergence", label: "Divergence", sections: ["evidence"] },
  { id: "patterns", label: "Patterns", sections: ["patterns", "current"] },
  { id: "breakouts", label: "Breakout events", sections: [] },
  { id: "evidence", label: "Volume and volatility", sections: ["evidence"] },
];

/** Sections every chart loads first (ADR-0027 §6). */
export const BASE_SECTIONS: readonly Section[] = ["identity", "versions", "current", "provenance"];

export type LayerSettings = {
  enabled: ReadonlySet<LayerId>;
  averages: readonly string[];
  oscillator: keyof typeof OSCILLATORS;
  swings: SwingSelection;
  fibonacci: Selection;
  patterns: Selection;
  breakouts: "pattern" | "level";
};

export const DEFAULT_SETTINGS: LayerSettings = {
  enabled: new Set(),
  averages: ["sma_10", "sma_40"],
  oscillator: "rsi",
  swings: "primary",
  fibonacci: "current",
  patterns: "current",
  breakouts: "pattern",
};

export function sectionsFor(settings: LayerSettings): Section[] {
  const out = new Set<Section>();
  for (const spec of LAYERS) if (settings.enabled.has(spec.id)) for (const s of spec.sections) out.add(s);
  if (settings.enabled.has("divergence") && settings.enabled.has("oscillator")) out.add("indicators");
  return [...out];
}

export type LayerRun = { results: LayerResult[]; refusedCount: number };

/** Run every enabled layer whose sections are present. Pure. */
export function runLayers(
  frame: Frame,
  doc: ChartDocument,
  settings: LayerSettings,
  breakoutRows: readonly BreakoutRow[] | null,
): LayerRun {
  const on = (id: LayerId) => settings.enabled.has(id);
  const results: LayerResult[] = [];
  const oscillatorSeries = on("oscillator") ? OSCILLATORS[settings.oscillator] ?? [] : [];
  if (doc.indicators) {
    if (on("averages")) results.push(indicatorLayer(frame, "averages", doc.indicators, settings.averages));
    if (on("bollinger")) results.push(indicatorLayer(frame, "bollinger", doc.indicators, BOLLINGER));
    if (on("oscillator")) results.push(indicatorLayer(frame, "oscillator", doc.indicators, oscillatorSeries));
  }
  if (doc.swings && on("swings")) results.push(swingLayer(frame, doc.swings, settings.swings));
  if (doc.structure && on("structure")) results.push(structureLayer(frame, doc.structure));
  if (doc.structure && on("trend")) results.push(trendLayer(frame, doc.structure));
  if (doc.levels && on("zones")) results.push(zoneLayer(frame, doc.levels));
  if (doc.levels && on("trendlines")) results.push(trendlineLayer(frame, doc.levels));
  if (doc.fibonacci && doc.current && on("fibonacci")) {
    results.push(fibonacciLayer(frame, doc.fibonacci, doc.current, settings.fibonacci));
  }
  if (doc.evidence && on("divergence")) results.push(divergenceLayer(frame, doc.evidence, oscillatorSeries));
  let patterns: LayerResult | null = null;
  if (doc.patterns && doc.current && on("patterns")) {
    patterns = patternLayer(frame, doc.patterns, doc.current, settings.patterns);
    results.push(patterns);
  }
  if (breakoutRows && on("breakouts")) results.push(breakoutLayer(frame, breakoutRows));
  if (doc.evidence && on("evidence")) {
    const drawnIds = new Set(patterns?.drawn.map((d) => d.id) ?? []);
    const drawnPatterns = (doc.patterns?.patterns ?? []).filter((p) => drawnIds.has(p.pattern_id));
    results.push(evidenceLayer(frame, doc.evidence, citedEvidence(drawnPatterns)));
  }
  return { results, refusedCount: results.reduce((n, r) => n + r.refused.length, 0) };
}

/** Every layer with its widest stored selection: used to count unplaced objects over a
 * whole document (the real-data checkpoint), not by the page. */
export const ALL_ON: LayerSettings = {
  enabled: new Set(LAYERS.map((l) => l.id)),
  averages: [],
  oscillator: "rsi",
  swings: "primary",
  fibonacci: "all",
  patterns: "all",
  breakouts: "pattern",
};
