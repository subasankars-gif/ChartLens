/**
 * The current weekly state, as the published analysis states it (Issue 2).
 *
 * Everything here *selects* authoritative objects of the chart's own snapshot; nothing
 * is computed, ranked, merged or reinterpreted (ADR-0027 "6d may select and render
 * authoritative analytical objects"):
 *
 * * "Current" is the engine's: `current.state_date` is the last complete weekly bar
 *   (the forming week never confirms anything), and the current objects are the ones
 *   the published explanation restates (ADR-0028: current state only, engine order).
 * * Claims are grouped by their stored `claim_type`, in the stored order. The engine
 *   already writes them category by category, so grouping never reorders a claim; if a
 *   category ever reappeared later in the list, it simply starts a new group.
 * * "Changes since the current trend state began" are the stored structure events and
 *   the stored breakout events whose `bar_date` is on or after the engine's own
 *   `current.trend_since`. Pattern and level breakout events stay in separate lists.
 */

import type { BreakoutRow, ChartDocument, StructureEvent } from "./layers/document";

export type ClaimGroup = "context" | "trend" | "levels" | "fibonacci" | "patterns" | "other";

export const GROUP_TITLE: Record<ClaimGroup, string> = {
  context: "Data",
  trend: "Trend and market structure",
  levels: "Support, resistance and trendlines",
  fibonacci: "Fibonacci",
  patterns: "Patterns and their stored conditions",
  other: "Other",
};

/** The sections the current-state view needs besides the chart's base sections. */
export const CURRENT_STATE_SECTIONS = ["structure"] as const;

export function claimGroup(claimType: string): ClaimGroup {
  if (claimType.startsWith("DATA_CONTEXT") || claimType === "FORMING_WEEK") return "context";
  if (claimType.startsWith("TREND_STATE")) return "trend";
  if (claimType.startsWith("ZONE") || claimType === "ACTIVE_TRENDLINE") return "levels";
  if (claimType.startsWith("FIBONACCI")) return "fibonacci";
  if (claimType.startsWith("PATTERN")) return "patterns";
  return "other";
}

/** Consecutive runs of the same group, in the stored order (never reordered). */
export function groupInOrder<T extends { claim_type: string }>(claims: readonly T[]): { group: ClaimGroup; claims: T[] }[] {
  const out: { group: ClaimGroup; claims: T[] }[] = [];
  for (const c of claims) {
    const g = claimGroup(c.claim_type);
    const last = out.at(-1);
    if (last && last.group === g) last.claims.push(c);
    else out.push({ group: g, claims: [c] });
  }
  return out;
}

/** Stored structure events on or after the engine's trend `since` date, stored order. */
export function structureChangesSince(doc: ChartDocument, since: string | null): StructureEvent[] {
  if (!since || !doc.structure) return [];
  return doc.structure.events.filter((e) => e.bar_date >= since);
}

/** Stored breakout rows on or after `since`, stored order (one source at a time). */
export function breakoutsSince(rows: readonly BreakoutRow[], since: string | null): BreakoutRow[] {
  if (!since) return [];
  return rows.filter((r) => r.bar_date >= since);
}

/** A snapshot older than this is shown as stale (a presentation threshold, not analysis). */
export const STALE_AFTER_DAYS = 4;
