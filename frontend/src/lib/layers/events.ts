/**
 * Divergences, breakout events and volume/volatility/candle evidence.
 *
 * * Divergence: (`date_start`, `price_1`) to (`date_end`, `price_2`) on price and, when
 *   the oscillator pane shows its indicator, (`date_start`, `indicator_1`) to
 *   (`date_end`, `indicator_2`) there. Status: the last stored entry.
 * * Breakout events (rows of one published dataset): a marker at (`bar_date`,
 *   `level_at_break`) and a time mark at each stored follow-up's `effective_date`.
 * * Volume and volatility events: time marks at `bar_date`. Candle events only when a
 *   drawn pattern cites them (ADR-0023).
 */

import { value4, words } from "./display";
import type { BreakoutRow, EvidenceEvent, EvidenceSection } from "./document";
import { admit, at } from "./place";
import type { Candidate, Frame, LayerResult, Primitive } from "./types";

export function divergenceCandidates(section: EvidenceSection, oscillatorSeries: readonly string[]): Candidate[] {
  return section.divergence.divergences.map((d) => {
    const last = d.status_history.at(-1);
    const bullish = d.type.includes("BULLISH");
    const role = bullish ? "divergence-bull" : "divergence-bear";
    const primitives: Primitive[] = [
      { kind: "path", pane: "price", points: [at(d.date_start, d.price_1), at(d.date_end, d.price_2)], role, dashed: false },
    ];
    if (oscillatorSeries.includes(d.indicator)) {
      primitives.push({
        kind: "path",
        pane: "oscillator",
        points: [at(d.date_start, d.indicator_1), at(d.date_end, d.indicator_2)],
        role,
        dashed: false,
      });
    }
    return {
      id: d.divergence_id,
      segmentId: d.continuity_segment_id,
      knownAt: d.known_at,
      title: `${words(d.type)} divergence (${d.indicator})`,
      status: last?.status,
      provisional: last?.provisional ?? false,
      details: [
        ["Status", words(last?.status)],
        ["Price", `${value4(d.price_1)} (${d.date_start}) to ${value4(d.price_2)} (${d.date_end})`],
        [d.indicator, `${value4(d.indicator_1)} to ${value4(d.indicator_2)}`],
        ["Known", d.known_at],
      ],
      primitives,
    };
  });
}

export function divergenceLayer(frame: Frame, section: EvidenceSection, oscillatorSeries: readonly string[]): LayerResult {
  return admit(frame, "divergence", divergenceCandidates(section, oscillatorSeries));
}

export function breakoutCandidates(rows: readonly BreakoutRow[]): Candidate[] {
  return rows.map((r) => ({
    id: r.event_id,
    segmentId: r.continuity_segment_id,
    knownAt: r.known_at,
    alsoKnownAt: r.history.map((h) => h.known_at),
    title: `${words(r.direction)} of ${r.pattern_type ? words(r.pattern_type) : `${words(r.level_source_type)} level`}`,
    status: r.history.at(-1)?.kind,
    provisional: r.provisional,
    details: [
      ["Level at break", value4(r.level_at_break)],
      ["Bar", r.bar_date],
      ["Known", r.known_at],
      ["Follow-ups", r.history.map((h) => `${words(h.kind)} ${h.effective_date}`).join(", ") || "—"],
      ["Source", r.source_id],
    ],
    primitives: [
      {
        kind: "dot",
        pane: "price",
        at: at(r.bar_date, r.level_at_break),
        role: r.direction === "BREAKOUT" ? "breakout-up" : "breakout-down",
        hollow: r.provisional,
        above: r.direction === "BREAKOUT",
      },
      ...r.history.map((h) => ({ kind: "tick" as const, date: h.effective_date, role: "follow-up" as const, text: words(h.kind) })),
    ],
  }));
}

export function breakoutLayer(frame: Frame, rows: readonly BreakoutRow[]): LayerResult {
  return admit(frame, "breakouts", breakoutCandidates(rows));
}

function evidenceCandidate(e: EvidenceEvent, family: string): Candidate {
  const name = e.kind ?? e.type ?? family;
  return {
    id: e.event_id,
    segmentId: e.continuity_segment_id,
    knownAt: e.known_at,
    title: `${words(name)} (${family})`,
    provisional: e.provisional,
    details: [["Bar", e.bar_date], ...(e.description ? ([["Note", e.description]] as const) : [])],
    primitives: [{ kind: "tick", date: e.bar_date, role: "evidence", text: words(name) }],
  };
}

export function evidenceCandidates(section: EvidenceSection, citedCandleIds: ReadonlySet<string>): Candidate[] {
  return [
    ...section.volume.events.map((e) => evidenceCandidate(e, "volume")),
    ...section.volatility.events.map((e) => evidenceCandidate(e, "volatility")),
    ...section.candles.events.filter((e) => citedCandleIds.has(e.event_id)).map((e) => evidenceCandidate(e, "candle")),
  ];
}

export function evidenceLayer(frame: Frame, section: EvidenceSection, citedCandleIds: ReadonlySet<string>): LayerResult {
  return admit(frame, "evidence", evidenceCandidates(section, citedCandleIds));
}
