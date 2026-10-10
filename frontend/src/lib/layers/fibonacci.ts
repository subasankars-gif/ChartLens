/**
 * Fibonacci structures (default: the engine's `current.fibonacci_ids`; "all" selects
 * every stored structure). Each is drawn as its stored leg, (`anchor_bar_date`,
 * `anchor_price`) to (`counter_bar_date`, `counter_price`), and each stored level as its
 * stored `price` from the later of `counter_bar_date` and the structure's `known_at` (a
 * level is authoritative only once known) to the end of its stored span: the section's
 * `state_date` for a structure the engine lists as current, otherwise the date of its
 * last stored status entry.
 */

import { value4, words } from "./display";
import type { CurrentSection, FibonacciSection } from "./document";
import { admit, at, later } from "./place";
import type { Candidate, Frame, LayerResult, Primitive } from "./types";

export type Selection = "current" | "all";

export function fibonacciCandidates(section: FibonacciSection, current: CurrentSection, selection: Selection): Candidate[] {
  const listed = new Set(current.fibonacci_ids);
  return section.structures
    .filter((f) => selection === "all" || listed.has(f.fib_id))
    .map((f) => {
      const last = f.status_history.at(-1);
      const end = listed.has(f.fib_id) ? section.state_date : (last?.date ?? f.counter_bar_date);
      const levels: Primitive[] = f.levels.map((l) => ({
        kind: "path",
        pane: "price",
        points: [at(later(f.counter_bar_date, f.known_at), l.price), at(end, l.price)],
        role: "fib-level",
        dashed: last?.provisional ?? false,
        label: String(l.ratio),
        level: true,
      }));
      return {
        id: f.fib_id,
        segmentId: f.continuity_segment_id,
        knownAt: f.known_at,
        title: `Fibonacci ${words(f.direction)} leg (${f.method}, ${words(f.sensitivity)})`,
        status: last?.status,
        provisional: last?.provisional ?? false,
        details: [
          ["Status", words(last?.status)],
          ["Leg", `${value4(f.anchor_price)} (${f.anchor_bar_date}) to ${value4(f.counter_price)} (${f.counter_bar_date})`],
          ["Known", f.known_at],
          ["Levels", f.levels.map((l) => `${l.ratio}: ${value4(l.price)}`).join(", ")],
        ],
        primitives: [
          {
            kind: "path",
            pane: "price",
            points: [at(f.anchor_bar_date, f.anchor_price), at(f.counter_bar_date, f.counter_price)],
            role: "fib-leg",
            dashed: true,
          },
          ...levels,
        ],
      };
    });
}

export function fibonacciLayer(
  frame: Frame,
  section: FibonacciSection,
  current: CurrentSection,
  selection: Selection,
): LayerResult {
  return admit(frame, "fibonacci", fibonacciCandidates(section, current, selection));
}
