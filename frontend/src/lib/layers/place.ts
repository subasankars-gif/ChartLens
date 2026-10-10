/**
 * The adapter boundary (ADR-0027 §2, §4, §5). One function decides whether a proposed
 * object may be drawn, and it never moves anything:
 *
 * * **Time.** The object must have a stored `known_at` ≤ the snapshot's `as_of` (and so
 *   must every stored knowability date it depends on). Its geometry stays at its stored
 *   formation coordinates; `known_at` affects visibility and annotation, not geometry.
 * * **Segment.** It must belong to the current continuity segment.
 * * **Bars.** Every stored date it would be drawn at must be the `last_session_date` of a
 *   bar of that segment, exactly. A date that is not is never snapped to a nearby bar:
 *   the whole object is refused and counted.
 * * **Levels.** A level (a confirmation or invalidation level, a Fibonacci level, a zone,
 *   a measured-move zone) is authoritative only from the object's `known_at`: no date it
 *   is drawn at may precede that date. Formation geometry (pattern lines, key points,
 *   legs, touches) stays at its stored formation coordinates.
 * * **Values.** Every value must be a finite stored number.
 *
 * Refused objects are surfaced, never dropped silently.
 */

import type { WeeklyResponse } from "../api";
import type { Candidate, Coord, Drawn, Frame, LayerId, LayerResult, Primitive, Refusal, RefusalReason } from "./types";

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

export function frameOf(weekly: Pick<WeeklyResponse, "bars" | "current_segment_id" | "as_of">): Frame {
  const barDates = new Set<string>();
  for (const bar of weekly.bars) {
    if (bar.continuity_segment_id === weekly.current_segment_id) barDates.add(bar.last_session_date);
  }
  return { segmentId: weekly.current_segment_id, asOf: weekly.as_of, barDates };
}

/** Every stored date a primitive is drawn at. */
export function primitiveDates(p: Primitive): string[] {
  switch (p.kind) {
    case "path":
      return p.points.map((c) => c.date);
    case "box":
      return [p.from.date, p.to.date];
    case "dot":
      return [p.at.date];
    case "tick":
      return [p.date];
    case "span":
      return [p.from, p.to];
  }
}

/** Every stored coordinate a primitive carries (time-only marks have none). */
export function primitiveCoords(p: Primitive): Coord[] {
  switch (p.kind) {
    case "path":
      return [...p.points];
    case "box":
      return [p.from, p.to];
    case "dot":
      return [p.at];
    case "tick":
    case "span":
      return [];
  }
}

function check(frame: Frame, c: Candidate): { reason: RefusalReason; detail: string } | null {
  if (c.segmentId !== frame.segmentId) {
    return { reason: "other_segment", detail: `segment ${c.segmentId}, chart segment ${frame.segmentId}` };
  }
  for (const known of [c.knownAt, ...(c.alsoKnownAt ?? [])]) {
    if (typeof known !== "string" || !ISO_DATE.test(known)) {
      return { reason: "known_at_missing", detail: `known_at ${String(known)}` };
    }
    if (known > frame.asOf) return { reason: "known_after_as_of", detail: `known ${known}, as of ${frame.asOf}` };
  }
  for (const p of c.primitives) {
    if ((p.kind === "path" || p.kind === "box") && p.level) {
      const early = primitiveDates(p).find((d) => d < (c.knownAt as string));
      if (early !== undefined) {
        return { reason: "level_before_known", detail: `level at ${early}, known ${c.knownAt}` };
      }
    }
    for (const coord of primitiveCoords(p)) {
      if (typeof coord.value !== "number" || !Number.isFinite(coord.value)) {
        return { reason: "bad_value", detail: `value ${String(coord.value)} at ${coord.date}` };
      }
    }
    for (const date of primitiveDates(p)) {
      if (typeof date !== "string" || !frame.barDates.has(date)) {
        return { reason: "date_not_on_a_bar", detail: `date ${String(date)}` };
      }
    }
  }
  return null;
}

/** Place a layer's candidates: each is drawn whole, or refused whole and counted. */
export function admit(frame: Frame, layer: LayerId, candidates: readonly Candidate[]): LayerResult {
  const drawn: Drawn[] = [];
  const refused: Refusal[] = [];
  for (const c of candidates) {
    const problem = check(frame, c);
    if (problem) refused.push({ id: c.id, layer, ...problem });
    else drawn.push({ ...c, layer, knownAt: c.knownAt as string });
  }
  return { layer, drawn, refused };
}

/** The later of two stored ISO dates (a choice between stored dates, not arithmetic):
 * where a level becomes visible, `later(formation start, known_at)`. */
export function later(a: string, b: string): string {
  return a >= b ? a : b;
}

/** A stored (date, value) pair, unchanged. The only way adapters build coordinates. */
export function at(date: string, value: number): Coord {
  return { date, value };
}
