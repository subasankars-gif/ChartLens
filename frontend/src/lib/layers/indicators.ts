/**
 * Indicator layers: moving averages and Bollinger bands on the price pane, one
 * oscillator in its own pane. Each value is placed at the section's own `bar_dates[i]`
 * with `data[i]` (the stored pairing); a null value is no point, and a line never
 * bridges a null (that would draw values the engine did not compute). Each value is
 * computed from bars through its own bar date (the engine's causal contract), so its
 * knowability date is that bar date. Values at provisional (forming-week) bars are drawn
 * dashed.
 */

import { admit, at } from "./place";
import type { Candidate, Coord, Frame, LayerId, LayerResult, Pane, Primitive, Role } from "./types";
import type { IndicatorsSection } from "./document";

export const OSCILLATORS: Record<string, readonly string[]> = {
  rsi: ["rsi"],
  macd: ["macd", "macd_signal"],
  stochastic: ["stochastic_k", "stochastic_d"],
};

export const BOLLINGER = ["bollinger_upper", "bollinger_middle", "bollinger_lower"] as const;

/** Stored series that are moving averages, in the engine's order. */
export function averageNames(section: IndicatorsSection): string[] {
  return section.series.map((s) => s.name).filter((n) => /^(sma|ema)_\d+$/.test(n));
}

function runs(section: IndicatorsSection, data: readonly (number | string | null)[], pane: Pane, role: Role): Primitive[] {
  const out: Primitive[] = [];
  let current: Coord[] = [];
  let currentProvisional = false;
  const flush = () => {
    if (current.length) out.push({ kind: "path", pane, points: current, role, dashed: currentProvisional });
    current = [];
  };
  section.bar_dates.forEach((date, i) => {
    const v = data[i];
    if (typeof v !== "number") {
      flush();
      return;
    }
    const provisional = section.provisional[i] === true;
    if (current.length && provisional !== currentProvisional) {
      // The dashed part starts at the last regular point (both stored).
      const last = current.at(-1)!;
      flush();
      current = [last];
    }
    currentProvisional = provisional;
    current.push(at(date, v));
  });
  flush();
  return out;
}

export function indicatorCandidates(
  section: IndicatorsSection,
  names: readonly string[],
  pane: Pane,
  role: (name: string) => Role,
): Candidate[] {
  const out: Candidate[] = [];
  for (const name of names) {
    const series = section.series.find((s) => s.name === name);
    if (!series) continue;
    const dates = section.bar_dates.filter((_, i) => typeof series.data[i] === "number");
    out.push({
      id: `indicators:${name}`,
      segmentId: section.context.continuity_segment_id,
      knownAt: dates[0] ?? null,
      alsoKnownAt: dates,
      title: name,
      provisional: false,
      details: [["Series", name]],
      primitives: runs(section, series.data, pane, role(name)),
    });
  }
  return out;
}

export function indicatorLayer(
  frame: Frame,
  layer: LayerId,
  section: IndicatorsSection,
  names: readonly string[],
): LayerResult {
  const pane: Pane = layer === "oscillator" ? "oscillator" : "price";
  const role = (name: string): Role =>
    layer === "bollinger"
      ? "band"
      : layer === "oscillator"
        ? name.endsWith("_signal") || name.endsWith("_d")
          ? "signal"
          : "oscillator"
        : "average";
  return admit(frame, layer, indicatorCandidates(section, names, pane, role));
}
