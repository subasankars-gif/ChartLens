/**
 * Swings, market structure and trend state.
 *
 * * Swings of one stored method and sensitivity (default: the engine's
 *   `primary_method`/`primary_sensitivity`), each at its (`bar_date`, `price`); hollow
 *   when provisional; its confirmation date is its `known_at`.
 * * Structure labels at (`bar_date`, `price`); BOS/CHoCH at (`bar_date`, `level`), with a
 *   time mark at `known_at` when it differs.
 * * Trend state: each `trend_history` entry from its `since` to the next entry's `since`
 *   (the last to the section's `as_of`). The engine defines the state as of T as the last
 *   entry with `since` ≤ T, so `since` is when the state became known.
 *
 * Pending (developing) extremes are not drawn: the engine stores them with no
 * `known_at` (unconfirmed), and the chart draws only objects with a stored `known_at`
 * ≤ the snapshot's `as_of` (ADR-0027 §4).
 */

import { sentence, value4, words } from "./display";
import type { StructureSection, SwingsSection } from "./document";
import { admit, at } from "./place";
import type { Candidate, Frame, LayerResult, Role } from "./types";

export type SwingSelection = { method: string; sensitivity: string } | "primary";

export function swingChoices(section: SwingsSection): { method: string; sensitivity: string }[] {
  const seen = new Map<string, { method: string; sensitivity: string }>();
  for (const s of section.swings) seen.set(`${s.method}|${s.sensitivity}`, { method: s.method, sensitivity: s.sensitivity });
  return [...seen.values()];
}

export function swingCandidates(section: SwingsSection, selection: SwingSelection): Candidate[] {
  const method = selection === "primary" ? section.primary_method : selection.method;
  const sensitivity = selection === "primary" ? section.primary_sensitivity : selection.sensitivity;
  return section.swings
    .filter((s) => s.method === method && s.sensitivity === sensitivity)
    .map((s) => ({
      id: s.swing_id,
      segmentId: s.continuity_segment_id,
      knownAt: s.known_at,
      title: `Swing ${s.type === "HIGH" ? "high" : "low"} (${s.method}, ${words(s.sensitivity)})`,
      provisional: s.provisional,
      details: [
        ["Price", value4(s.price)],
        ["Formed", s.bar_date],
        ["Confirmed", s.known_at ?? "—"],
        ...(s.provisional ? ([["Provisional", "confirmed by a week closing on a non-regular session"]] as const) : []),
      ],
      primitives: [
        {
          kind: "dot",
          pane: "price",
          at: at(s.bar_date, s.price),
          role: s.type === "HIGH" ? "swing-high" : "swing-low",
          hollow: s.provisional,
          above: s.type === "HIGH",
        },
      ],
    }));
}

export function swingLayer(frame: Frame, section: SwingsSection, selection: SwingSelection): LayerResult {
  return admit(frame, "swings", swingCandidates(section, selection));
}

export function structureCandidates(section: StructureSection): Candidate[] {
  const segment = section.context.continuity_segment_id;
  const labels: Candidate[] = section.labels.map((l) => ({
    id: `${l.swing_id}:LABEL`,
    segmentId: segment,
    knownAt: l.known_at,
    title: `${l.label} (${words(l.type)})`,
    provisional: false,
    details: [
      ["Price", value4(l.price)],
      ["Formed", l.bar_date],
      ["Known", l.known_at],
      ["Swings", `${section.swing_method}, ${words(section.swing_sensitivity)}`],
    ],
    primitives: [
      {
        kind: "dot",
        pane: "price",
        at: at(l.bar_date, l.price),
        role: "label",
        hollow: true,
        text: l.label,
        above: l.type === "HIGH",
      },
    ],
  }));
  const events: Candidate[] = section.events.map((e) => ({
    id: e.event_id,
    segmentId: e.continuity_segment_id,
    knownAt: e.known_at,
    title: `${e.kind} ${words(e.direction)}`,
    provisional: e.provisional,
    details: [
      ["Level", value4(e.level)],
      ["Bar", e.bar_date],
      ["Known", e.known_at],
    ],
    primitives: [
      {
        kind: "dot",
        pane: "price",
        at: at(e.bar_date, e.level),
        role: e.kind === "BOS" ? "bos" : "choch",
        hollow: e.provisional,
        text: e.kind,
        above: e.direction === "UP",
      },
      ...(e.known_at !== e.bar_date
        ? [{ kind: "tick" as const, date: e.known_at, role: "recognised" as Role, text: `${e.kind} known` }]
        : []),
    ],
  }));
  return [...labels, ...events];
}

export function structureLayer(frame: Frame, section: StructureSection): LayerResult {
  return admit(frame, "structure", structureCandidates(section));
}

function trendRole(state: string): Role {
  if (state.includes("UP")) return "trend-up";
  if (state.includes("DOWN")) return "trend-down";
  return "trend-range";
}

export function trendCandidates(section: StructureSection): Candidate[] {
  const history = section.trend_history;
  return history.map((t, i) => {
    const to = history[i + 1]?.since ?? section.context.as_of;
    return {
      id: `${section.context.continuity_segment_id}:TREND:${t.since}`,
      segmentId: section.context.continuity_segment_id,
      knownAt: t.since,
      title: sentence(t.state),
      status: t.state,
      provisional: t.provisional,
      details: [
        ["From", t.since],
        ["Until", history[i + 1] ? history[i + 1]!.since : "now"],
        ["Last event", t.last_event_id ?? "—"],
      ],
      primitives: [{ kind: "span", from: t.since, to, role: trendRole(t.state), text: sentence(t.state) }],
    };
  });
}

export function trendLayer(frame: Frame, section: StructureSection): LayerResult {
  return admit(frame, "trend", trendCandidates(section));
}
