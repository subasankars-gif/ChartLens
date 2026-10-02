"use client";

/**
 * Weekly candles and volume, drawn exactly as the API returned them (ADR-0017).
 * Each continuity segment is its own series; break bands sit between segments; the
 * forming week is hollow; special-session closes and split weeks are marked. The legend
 * shows the API's decimal text for the bar under the cursor (or the latest bar).
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type { WeeklyBar } from "@/lib/api";
import { buildChartModel, type Palette } from "@/lib/chart-model";
import { formatDate, formatDecimal, formatQuantity, sessionTypeLabel } from "@/lib/format";
import { BreakBands } from "./breakBands";

function cssVar(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

export function WeeklyChart({
  bars,
  currentSegmentId,
  causes,
}: {
  bars: WeeklyBar[];
  currentSegmentId: string;
  causes: ReadonlyMap<string, string>;
}) {
  const container = useRef<HTMLDivElement>(null);
  const [hovered, setHovered] = useState<string | null>(null);

  const palette: Palette = useMemo(
    () => ({
      up: cssVar("--color-up", "#1c7a4d"),
      down: cssVar("--color-down", "#b02a30"),
      upMuted: cssVar("--color-up-muted", "#9cc3ae"),
      downMuted: cssVar("--color-down-muted", "#dda6a9"),
      forming: cssVar("--color-warn", "#96580a"),
    }),
    [],
  );
  const model = useMemo(() => buildChartModel(bars, currentSegmentId, causes, palette), [
    bars,
    currentSegmentId,
    causes,
    palette,
  ]);

  useEffect(() => {
    const el = container.current;
    if (!el) return;
    let disposed = false;
    let cleanup = () => {};
    void import("lightweight-charts").then((lc) => {
      if (disposed) return;
      const ink = cssVar("--color-ink", "#16222c");
      const muted = cssVar("--color-muted", "#5d6c78");
      const line = cssVar("--color-line", "#d8dfe4");
      const chart = lc.createChart(el, {
        autoSize: true,
        layout: {
          background: { type: lc.ColorType.Solid, color: cssVar("--color-surface", "#ffffff") },
          textColor: muted,
          fontFamily: "'IBM Plex Sans', system-ui, sans-serif",
          panes: { separatorColor: line },
          attributionLogo: false,
        },
        grid: { vertLines: { color: line }, horzLines: { color: line } },
        rightPriceScale: { borderColor: line },
        timeScale: { borderColor: line, barSpacing: 7, rightOffset: 4 },
        crosshair: { horzLine: { labelBackgroundColor: ink }, vertLine: { labelBackgroundColor: ink } },
      });
      const candleSeries = model.series.map((s) => {
        const series = chart.addSeries(lc.CandlestickSeries, {
          priceLineVisible: s.isCurrent,
          lastValueVisible: s.isCurrent,
        });
        series.setData(s.candles.map((c) => ({ ...c, time: c.time as import("lightweight-charts").Time })));
        const volume = chart.addSeries(
          lc.HistogramSeries,
          { priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false },
          1,
        );
        volume.setData(s.volumes.map((v) => ({ ...v, time: v.time as import("lightweight-charts").Time })));
        const own = model.markers.filter((m) => s.candles.some((c) => c.time === m.time));
        lc.createSeriesMarkers(
          series,
          own.map((m) => ({
            time: m.time as import("lightweight-charts").Time,
            position: m.kind === "special" ? "belowBar" : "aboveBar",
            shape: m.kind === "forming" ? "circle" : "square",
            color: m.kind === "partial" ? cssVar("--color-break", "#7d8a94") : cssVar("--color-warn", "#96580a"),
            text: m.text,
            size: 0.6,
          })),
        );
        return series;
      });
      const last = candleSeries.at(-1);
      if (last && model.breaks.length) {
        last.attachPrimitive(new BreakBands(model.breaks, cssVar("--color-break", "#7d8a94"), ink));
      }
      chart.panes()[1]?.setHeight(90);
      // Open on roughly the last three years; the full history is a scroll or zoom away.
      const total = model.barsByTime.size;
      if (total > 160) chart.timeScale().setVisibleLogicalRange({ from: total - 156, to: total + 4 });
      else chart.timeScale().fitContent();
      chart.subscribeCrosshairMove((p) => setHovered(p.time ? String(p.time) : null));
      cleanup = () => chart.remove();
    });
    return () => {
      disposed = true;
      cleanup();
    };
  }, [model]);

  const shown = (hovered && model.barsByTime.get(hovered)) || bars.at(-1);

  return (
    <figure className="rounded-lg border border-line bg-surface">
      <figcaption className="flex flex-wrap items-baseline gap-x-5 gap-y-1 border-b border-line px-4 py-2.5 text-sm">
        {shown ? <Legend bar={shown} /> : <span className="text-muted">No weekly bars.</span>}
      </figcaption>
      <div ref={container} className="h-[520px] w-full" data-testid="weekly-chart" />
      <ChartKey />
    </figure>
  );
}

function Legend({ bar }: { bar: WeeklyBar }) {
  const flags: string[] = [];
  if (!bar.is_complete) flags.push("Forming week, not closed");
  if (bar.partial_reason === "CONTINUITY_BREAK") flags.push("Week split at a continuity break");
  if (bar.closes_on_special_session) flags.push(`Close from a ${sessionTypeLabel(bar.closing_session_type).toLowerCase()}`);
  return (
    <>
      <span className="font-medium" data-testid="legend-range">
        {formatDate(bar.first_session_date)} to {formatDate(bar.last_session_date)}
      </span>
      <span className="num" data-testid="legend-ohlc">
        O {formatDecimal(bar.open)} H {formatDecimal(bar.high)} L {formatDecimal(bar.low)} C{" "}
        {formatDecimal(bar.close)}
      </span>
      <span className="num text-muted">Vol {formatQuantity(bar.volume)}</span>
      <span className="num text-muted">
        {bar.trading_days} {bar.trading_days === 1 ? "session" : "sessions"}, traded close {formatDecimal(bar.raw_close)}
      </span>
      {flags.map((f) => (
        <span key={f} className="text-warn" data-testid="legend-flag">
          {f}
        </span>
      ))}
    </>
  );
}

function ChartKey() {
  return (
    <p className="border-t border-line px-4 py-2 text-xs leading-5 text-muted">
      Prices adjusted for splits, bonuses and rights; traded close shown as recorded. Hollow candle: the week is still
      forming. Split: a week cut by a continuity break. M, B, D, S: the week closed on a Muhurat, Budget-day, DR-drill
      or other special session. Hatched band: a continuity break; no price movement is implied across it.
    </p>
  );
}
