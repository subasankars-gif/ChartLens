/**
 * Weekly bars from the API → what the chart draws (ADR-0017). Pure and analysis-free:
 * it decides colours, markers and where continuity breaks are drawn, nothing else.
 *
 * * One candle per API bar, labelled by its last session. Prices become numbers only to
 *   be placed on the canvas; every displayed value is the API's own decimal text.
 * * Each continuity segment is its own series, so nothing ever joins two segments;
 *   earlier segments are drawn muted, and a break band marks every boundary.
 * * The forming week is hollow; split weeks and weeks closing on a special session are
 *   marked with what the API says about them.
 */

import type { WeeklyBar } from "./api";
import { sessionTypeLabel } from "./format";

export type Palette = { up: string; down: string; upMuted: string; downMuted: string; forming: string };

export type Candle = {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  color: string;
  borderColor: string;
  wickColor: string;
};

export type VolumeBar = { time: string; value: number; color: string };

export type SeriesModel = {
  segmentId: string;
  isCurrent: boolean;
  candles: Candle[];
  volumes: VolumeBar[];
};

export type Marker = {
  time: string;
  kind: "forming" | "partial" | "special";
  text: string;
  title: string;
};

export type BreakBand = { time: string; previousTime: string; cause: string };

export type ChartModel = {
  series: SeriesModel[];
  markers: Marker[];
  breaks: BreakBand[];
  barsByTime: Map<string, WeeklyBar>;
};

const SPECIAL_LETTER: Record<string, string> = { MUHURAT: "M", BUDGET: "B", DR_DRILL: "D", OTHER: "S" };

export function buildChartModel(
  bars: readonly WeeklyBar[],
  currentSegmentId: string,
  causes: ReadonlyMap<string, string>,
  palette: Palette,
): ChartModel {
  const series: SeriesModel[] = [];
  const markers: Marker[] = [];
  const breaks: BreakBand[] = [];
  const barsByTime = new Map<string, WeeklyBar>();

  for (const bar of bars) {
    const time = bar.last_session_date;
    if (barsByTime.has(time)) throw new Error(`two weekly bars labelled ${time}`);
    barsByTime.set(time, bar);

    let current = series.at(-1);
    if (!current || current.segmentId !== bar.continuity_segment_id) {
      const previous = current?.candles.at(-1);
      if (previous) {
        breaks.push({
          time,
          previousTime: previous.time,
          cause: causes.get(bar.continuity_segment_id) ?? "Continuity break",
        });
      }
      current = {
        segmentId: bar.continuity_segment_id,
        isCurrent: bar.continuity_segment_id === currentSegmentId,
        candles: [],
        volumes: [],
      };
      series.push(current);
    }

    const open = Number(bar.open);
    const close = Number(bar.close);
    const rising = close >= open;
    const solid = current.isCurrent
      ? rising
        ? palette.up
        : palette.down
      : rising
        ? palette.upMuted
        : palette.downMuted;
    const body = bar.is_complete ? solid : "transparent";
    const edge = bar.is_complete ? solid : palette.forming;
    current.candles.push({
      time,
      open,
      high: Number(bar.high),
      low: Number(bar.low),
      close,
      color: body,
      borderColor: edge,
      wickColor: edge,
    });
    current.volumes.push({ time, value: Number(bar.volume), color: solid });

    if (!bar.is_complete) {
      markers.push({
        time,
        kind: "forming",
        text: "Forming",
        title: "This week has not closed yet: it can still change and confirms nothing.",
      });
    }
    if (bar.partial_reason === "CONTINUITY_BREAK") {
      markers.push({
        time,
        kind: "partial",
        text: "Split",
        title: `Week split at a continuity break: ${bar.first_session_date} to ${bar.last_session_date}.`,
      });
    }
    if (bar.closes_on_special_session) {
      markers.push({
        time,
        kind: "special",
        text: SPECIAL_LETTER[bar.closing_session_type ?? "OTHER"] ?? "S",
        title: `Close from a ${sessionTypeLabel(bar.closing_session_type).toLowerCase()} (kept as traded).`,
      });
    }
  }
  return { series, markers, breaks, barsByTime };
}

/** The ranges a reader can pick; `null` weeks = every bar returned. */
export const RANGES: readonly { label: string; weeks: number | null }[] = [
  { label: "1Y", weeks: 52 },
  { label: "3Y", weeks: 156 },
  { label: "5Y", weeks: 260 },
  { label: "All", weeks: null },
];

/**
 * Which bars are in view: a logical range over *all* bars the API returned. The chart
 * always holds every bar; a range only scrolls the view (Issue 1: a recent view never
 * removes older history, which stays reachable by range, drag or zoom).
 *
 * With no choice made: the last 156 weeks, or 52 weeks before the last continuity break
 * when earlier segments are shown, so the break is in view.
 */
export function visibleRange(
  total: number,
  weeks: number | null | undefined,
  breakIndex = -1,
): { from: number; to: number } | null {
  if (total === 0) return null;
  if (weeks === null) return { from: 0, to: total + 4 };
  if (weeks === undefined) {
    const from = Math.max(0, Math.min(total - 156, breakIndex >= 0 ? breakIndex - 52 : total - 156));
    return total > 160 || from > 0 ? { from, to: total + 4 } : null; // null: fit everything
  }
  return { from: Math.max(0, total - weeks), to: total + 4 };
}
