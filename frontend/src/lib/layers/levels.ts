/**
 * Support/resistance zones and trendlines (levels section).
 *
 * * Zones (current only, by the engine's definition): a band from (`first_seen`,
 *   `price_low`) to (`levels.state_date`, `price_high`).
 * * Trendlines (ADR-0027 decision 1a): a line through the stored touches
 *   (`bar_date`, `line_value`) in stored order and, for a line the engine lists in
 *   `active_trendlines`, on to its stored `value` at `levels.state_date`. Any other line
 *   ends at its last touch. Nothing is extrapolated: the stored anchor price and slope are
 *   never used to compute a point.
 */

import { sentence, value4, words } from "./display";
import type { LevelsSection } from "./document";
import { admit, at } from "./place";
import type { Candidate, Frame, LayerResult } from "./types";

export function zoneCandidates(section: LevelsSection): Candidate[] {
  return section.zones.map((z) => ({
    id: z.zone_id,
    segmentId: z.continuity_segment_id,
    knownAt: z.known_at,
    title: `${sentence(z.type)} zone`,
    provisional: false,
    details: [
      ["Range", `${value4(z.price_low)} to ${value4(z.price_high)}`],
      ["First seen", z.first_seen],
      ["Known", z.known_at],
      ["Last tested", z.last_tested ?? "—"],
      ["Touches", String(z.touches.length)],
      ...(z.role_reversed ? ([["Role", "reversed"]] as const) : []),
    ],
    primitives: [
      {
        kind: "box",
        pane: "price",
        from: at(z.first_seen, z.price_low),
        to: at(section.state_date, z.price_high),
        role: z.type === "SUPPORT" ? "support" : "resistance",
      },
    ],
  }));
}

export function zoneLayer(frame: Frame, section: LevelsSection): LayerResult {
  return admit(frame, "zones", zoneCandidates(section));
}

export function trendlineCandidates(section: LevelsSection): Candidate[] {
  const active = new Map(section.active_trendlines.map((a) => [a.trendline_id, a.value]));
  return section.trendlines.map((t) => {
    const points = t.touches.map((touch) => at(touch.bar_date, touch.line_value));
    const activeValue = active.get(t.trendline_id);
    if (activeValue !== undefined) points.push(at(section.state_date, activeValue));
    const last = t.status_history.at(-1);
    return {
      id: t.trendline_id,
      segmentId: t.continuity_segment_id,
      knownAt: t.known_at,
      alsoKnownAt: t.touches.map((touch) => touch.known_at),
      title: `${sentence(t.type)} trendline`,
      status: last?.status,
      provisional: last?.provisional ?? false,
      details: [
        ["Status", words(last?.status)],
        ["Touches", String(t.touches.length)],
        ["Known", t.known_at],
        ...(activeValue !== undefined
          ? ([["Value now", `${value4(activeValue)} (${section.state_date})`]] as const)
          : ([["Drawn to", "its last stored touch"]] as const)),
      ],
      primitives: [{ kind: "path", pane: "price", points, role: "trendline", dashed: last?.provisional ?? false }],
    };
  });
}

export function trendlineLayer(frame: Frame, section: LevelsSection): LayerResult {
  return admit(frame, "trendlines", trendlineCandidates(section));
}
