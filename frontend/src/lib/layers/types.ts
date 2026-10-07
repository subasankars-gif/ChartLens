/**
 * Chart layers (ADR-0027): stored analytical objects → chart primitives.
 *
 * > 6d may select and render authoritative analytical objects; it must not infer,
 * > recompute, rank, merge, or reinterpret them.
 *
 * Every coordinate a primitive carries is a stored (date, value) pair of the object it
 * came from: `date` is a stored date string and `value` is the stored number itself,
 * never the result of arithmetic. Time-only marks (`tick`, `span`) carry stored dates and
 * no value. Placement on the chart is decided in one place, `admit` (place.ts), which
 * every layer goes through.
 */

export type LayerId =
  | "averages"
  | "bollinger"
  | "oscillator"
  | "swings"
  | "structure"
  | "trend"
  | "zones"
  | "trendlines"
  | "fibonacci"
  | "divergence"
  | "patterns"
  | "breakouts"
  | "evidence";

export type Pane = "price" | "oscillator";

/** A stored (date, value) pair. `value` is the stored number, untouched. */
export type Coord = { readonly date: string; readonly value: number };

/** Drawing roles: the renderer maps them to colours and styles. */
export type Role =
  | "average"
  | "band"
  | "oscillator"
  | "signal"
  | "swing-high"
  | "swing-low"
  | "label"
  | "bos"
  | "choch"
  | "trend-up"
  | "trend-down"
  | "trend-range"
  | "support"
  | "resistance"
  | "trendline"
  | "fib-leg"
  | "fib-level"
  | "divergence-bull"
  | "divergence-bear"
  | "pattern-point"
  | "pattern-line"
  | "pattern-confirmation"
  | "pattern-invalidation"
  | "measured-move"
  | "recognised"
  | "breakout-up"
  | "breakout-down"
  | "follow-up"
  | "evidence";

export type Primitive =
  /** A polyline through stored points, in the order given; never drawn past its ends. */
  | { kind: "path"; pane: Pane; points: readonly Coord[]; role: Role; dashed: boolean; label?: string }
  /** A rectangle spanning two stored corners: x from `from.date` to `to.date`, y from
   * `from.value` to `to.value`. */
  | { kind: "box"; pane: Pane; from: Coord; to: Coord; role: Role }
  /** A point marker at a stored coordinate. */
  | { kind: "dot"; pane: Pane; at: Coord; role: Role; hollow: boolean; text?: string; above: boolean }
  /** A time-only mark at a stored date (no price: the date is the information). */
  | { kind: "tick"; date: string; role: Role; text: string }
  /** A time-only band between two stored dates. */
  | { kind: "span"; from: string; to: string; role: Role; text: string };

/** One analytical object as the adapter proposes it, before placement. */
export type Candidate = {
  id: string;
  /** The object's stored segment (or its section's, for objects without one). */
  segmentId: string;
  /** The object's own stored `known_at` (null when the engine stored none). */
  knownAt: string | null;
  /** Further stored knowability dates the object depends on (touches, key points,
   * history entries), each checked against the snapshot's `as_of` too. */
  alsoKnownAt?: readonly (string | null)[];
  title: string;
  status?: string;
  provisional: boolean;
  /** Stored facts for the tooltip and evidence panel, as label/value pairs. */
  details: readonly (readonly [string, string])[];
  primitives: readonly Primitive[];
};

export type Drawn = Candidate & { layer: LayerId; knownAt: string };

export type RefusalReason = "date_not_on_a_bar" | "other_segment" | "known_at_missing" | "known_after_as_of" | "bad_value";

export type Refusal = { id: string; layer: LayerId; reason: RefusalReason; detail: string };

export type LayerResult = { layer: LayerId; drawn: Drawn[]; refused: Refusal[] };

/** What placement checks against: the current segment's bars and the snapshot's `as_of`. */
export type Frame = {
  segmentId: string;
  asOf: string;
  /** `last_session_date` of every bar of the current segment. */
  barDates: ReadonlySet<string>;
};

export const REFUSAL_TEXT: Record<RefusalReason, string> = {
  date_not_on_a_bar: "a stored date is not on a weekly bar of this chart",
  other_segment: "it belongs to another continuity segment",
  known_at_missing: "it has no stored known-at date",
  known_after_as_of: "it was known after this snapshot's date",
  bad_value: "a stored value is missing or not a number",
};
