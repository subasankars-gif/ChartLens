/**
 * Chart patterns (default: the engine's `current.included_pattern_ids`; "all" selects
 * every stored pattern), each from its stored geometry:
 *
 * * key points at (`bar_date`, `price`), labelled;
 * * lines from (`start_date`, `start_value`) to (`end_date`, `end_value`);
 * * the confirmation and invalidation levels, when stored, from the pattern's `known_at`
 *   (they are authoritative only once the pattern is recognised) to its next stored
 *   status entry's `effective_date` (the section's `as_of` while there is none);
 * * each stored measured-move zone, `target_low` to `target_high`, from the later of
 *   `target_calculated_at` and its status entry's `known_at` to the next status entry's `effective_date` (the section's
 *   `as_of` for the last);
 * * a "recognised" time mark at the pattern's `known_at`.
 *
 * The displayed status is the last stored status entry. Definition fit is shown with its
 * components and that wording, and never orders anything.
 */

import { sentence, value4, words } from "./display";
import type { CurrentSection, Pattern, PatternsSection } from "./document";
import { admit, at, later } from "./place";
import type { Candidate, Frame, LayerResult, Primitive } from "./types";
import type { Selection } from "./fibonacci";

function patternPrimitives(p: Pattern, sectionAsOf: string): Primitive[] {
  const last = p.status_history.at(-1);
  const dashed = last?.provisional ?? false;
  const out: Primitive[] = [];
  for (const line of p.geometry.lines) {
    out.push({
      kind: "path",
      pane: "price",
      points: [at(line.start_date, line.start_value), at(line.end_date, line.end_value)],
      role: "pattern-line",
      dashed,
      label: words(line.label),
    });
  }
  const { confirmation_level: confirmation, invalidation_level: invalidation } = p.geometry;
  const levelEnd = p.status_history[1]?.effective_date ?? sectionAsOf;
  if (confirmation !== null) {
    out.push({
      kind: "path",
      pane: "price",
      points: [at(p.known_at, confirmation), at(levelEnd, confirmation)],
      role: "pattern-confirmation",
      dashed: true,
      label: "confirmation",
      level: true,
    });
  }
  if (invalidation !== null) {
    out.push({
      kind: "path",
      pane: "price",
      points: [at(p.known_at, invalidation), at(levelEnd, invalidation)],
      role: "pattern-invalidation",
      dashed: true,
      label: "invalidation",
      level: true,
    });
  }
  for (const k of p.geometry.key_points) {
    out.push({
      kind: "dot",
      pane: "price",
      at: at(k.bar_date, k.price),
      role: "pattern-point",
      hollow: false,
      text: k.label.replaceAll("_", " ").toLowerCase(),
      above: true,
    });
  }
  p.status_history.forEach((entry, i) => {
    const mm = entry.measured_move;
    if (!mm) return;
    const until = p.status_history[i + 1]?.effective_date ?? sectionAsOf;
    out.push({
      kind: "box",
      pane: "price",
      // Visible from when this status entry was known (a pattern recognised after its
      // breakout has a zone calculated at the breakout bar but known later).
      from: at(later(mm.target_calculated_at, entry.known_at), mm.target_low),
      to: at(until, mm.target_high),
      role: "measured-move",
      level: true,
    });
  });
  out.push({ kind: "tick", date: p.known_at, role: "recognised", text: "Recognised" });
  return out;
}

export function patternCandidates(section: PatternsSection, current: CurrentSection, selection: Selection): Candidate[] {
  const listed = new Set(current.included_pattern_ids);
  return section.patterns
    .filter((p) => selection === "all" || listed.has(p.pattern_id))
    .map((p) => {
      const last = p.status_history.at(-1);
      const measured = p.status_history.filter((e) => e.measured_move).at(-1)?.measured_move;
      return {
        id: p.pattern_id,
        segmentId: p.continuity_segment_id,
        knownAt: p.known_at,
        alsoKnownAt: [...p.geometry.key_points.map((k) => k.known_at), ...p.status_history.map((e) => e.known_at)],
        title: sentence(p.pattern_type),
        status: last?.status,
        provisional: last?.provisional ?? false,
        details: [
          ["Status", `${words(last?.status)}${last ? ` (${last.effective_date})` : ""}`],
          ["Formed", `${p.start_date} to ${p.end_date}`],
          ["Recognised", p.known_at],
          ["Direction", words(p.direction)],
          ["Definition fit", String(p.definition_fit.value)],
          ...(measured
            ? ([["Measured move", `${value4(measured.target_low)} to ${value4(measured.target_high)}`]] as const)
            : []),
        ],
        primitives: patternPrimitives(p, section.context.as_of),
      };
    });
}

export function patternLayer(
  frame: Frame,
  section: PatternsSection,
  current: CurrentSection,
  selection: Selection,
): LayerResult {
  return admit(frame, "patterns", patternCandidates(section, current, selection));
}

/** Evidence ids a set of patterns cite (their context and status entries), as stored. */
export function citedEvidence(patterns: readonly Pattern[]): Set<string> {
  const out = new Set<string>();
  for (const p of patterns) {
    for (const ref of p.context.evidence_refs ?? []) out.add(ref);
    for (const e of p.status_history) for (const ref of e.evidence_refs) out.add(ref);
  }
  return out;
}
