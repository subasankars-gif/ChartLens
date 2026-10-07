"use client";

/**
 * Weekly candles and volume, drawn exactly as the API returned them (ADR-0017), with the
 * placed analytical layers on top (ADR-0027). Each continuity segment is its own series;
 * break bands sit between segments; the forming week is hollow; special-session closes
 * and split weeks are marked. Layers are drawn by `Overlay` from stored coordinates only,
 * and only over the current segment (placement guarantees it). An oscillator pane appears
 * when an oscillator layer is on. The legend shows the API's decimal text for the bar
 * under the cursor (or the latest bar) and the stored facts of the object under it.
 */

import type * as LC from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";
import type { WeeklyBar } from "@/lib/api";
import { buildChartModel, type Palette } from "@/lib/chart-model";
import { formatDate, formatDecimal, formatQuantity, sessionTypeLabel } from "@/lib/format";
import type { Drawn } from "@/lib/layers/types";
import { BreakBands } from "./breakBands";
import { Overlay, type RoleStyles } from "./overlay";

function cssVar(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

function roleStyles(): RoleStyles {
  const v = (n: string, f: string) => cssVar(n, f);
  const ink = v("--color-ink", "#16222c");
  const muted = v("--color-muted", "#5d6c78");
  const accent = v("--color-accent", "#1d5c99");
  const pattern = v("--color-layer-pattern", "#6b4fa3");
  const fib = v("--color-layer-fib", "#8a6d1f");
  const average = v("--color-layer-average", "#2f7f8f");
  const support = v("--color-layer-support", "#2f7f6a");
  const resistance = v("--color-layer-resistance", "#a8505a");
  const warn = v("--color-warn", "#96580a");
  return {
    average: { stroke: average, width: 1.25 },
    band: { stroke: muted, width: 1 },
    oscillator: { stroke: accent, width: 1.25 },
    signal: { stroke: warn, width: 1 },
    "swing-high": { stroke: ink },
    "swing-low": { stroke: ink },
    label: { stroke: muted },
    bos: { stroke: accent },
    choch: { stroke: warn },
    "trend-up": { stroke: v("--color-up-muted", "#9cc3ae") },
    "trend-down": { stroke: v("--color-down-muted", "#dda6a9") },
    "trend-range": { stroke: v("--color-line", "#d8dfe4") },
    support: { stroke: support, fill: support },
    resistance: { stroke: resistance, fill: resistance },
    trendline: { stroke: ink, width: 1.25 },
    "fib-leg": { stroke: fib, width: 1 },
    "fib-level": { stroke: fib, width: 1 },
    "divergence-bull": { stroke: v("--color-up", "#1c7a4d"), width: 1.75 },
    "divergence-bear": { stroke: v("--color-down", "#b02a30"), width: 1.75 },
    "pattern-point": { stroke: pattern },
    "pattern-line": { stroke: pattern, width: 1.75 },
    "pattern-confirmation": { stroke: pattern, width: 1 },
    "pattern-invalidation": { stroke: muted, width: 1 },
    "measured-move": { stroke: pattern, fill: pattern },
    recognised: { stroke: warn },
    "breakout-up": { stroke: accent },
    "breakout-down": { stroke: accent },
    "follow-up": { stroke: muted },
    evidence: { stroke: muted },
  };
}

type Live = {
  lc: typeof LC;
  chart: LC.IChartApi;
  price: Overlay;
  oscillator: { overlay: Overlay; carriers: LC.ISeriesApi<"Line">[] } | null;
};

const FONT = "500 11px 'IBM Plex Sans', system-ui, sans-serif";

export function WeeklyChart({
  bars,
  currentSegmentId,
  causes,
  drawn = [],
  highlighted = null,
  onHoverObject,
}: {
  bars: WeeklyBar[];
  currentSegmentId: string;
  causes: ReadonlyMap<string, string>;
  drawn?: readonly Drawn[];
  highlighted?: string | null;
  onHoverObject?: (id: string | null) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const live = useRef<Live | null>(null);
  const [ready, setReady] = useState(0);
  const [hovered, setHovered] = useState<string | null>(null);
  const [hoveredObject, setHoveredObject] = useState<string | null>(null);
  const hoverCallback = useRef(onHoverObject);
  useEffect(() => {
    hoverCallback.current = onHoverObject;
  }, [onHoverObject]);

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
        series.setData(s.candles.map((c) => ({ ...c, time: c.time as LC.Time })));
        const volume = chart.addSeries(
          lc.HistogramSeries,
          { priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false },
          1,
        );
        volume.setData(s.volumes.map((v) => ({ ...v, time: v.time as LC.Time })));
        const own = model.markers.filter((m) => s.candles.some((c) => c.time === m.time));
        lc.createSeriesMarkers(
          series,
          own.map((m) => ({
            time: m.time as LC.Time,
            position: m.kind === "special" ? "belowBar" : "aboveBar",
            shape: m.kind === "forming" ? "circle" : "square",
            color: m.kind === "partial" ? cssVar("--color-break", "#7d8a94") : cssVar("--color-warn", "#96580a"),
            text: m.text,
            size: 0.6,
          })),
        );
        return { series, isCurrent: s.isCurrent };
      });
      const last = candleSeries.at(-1)?.series;
      if (last && model.breaks.length) {
        last.attachPrimitive(new BreakBands(model.breaks, cssVar("--color-break", "#7d8a94"), ink));
      }
      // Overlays are drawn on the current segment's series: they never extend over an
      // earlier segment (placement refuses any date that is not one of its bars).
      const current = candleSeries.find((s) => s.isCurrent)?.series ?? last;
      const price = new Overlay("price", roleStyles(), FONT);
      current?.attachPrimitive(price);
      chart.panes()[1]?.setHeight(90);
      const times = [...model.barsByTime.keys()];
      const total = times.length;
      const lastBreak = model.breaks.at(-1);
      const breakIndex = lastBreak ? times.indexOf(lastBreak.time) : -1;
      const from = Math.max(0, Math.min(total - 156, breakIndex >= 0 ? breakIndex - 52 : total - 156));
      if (total > 160 || from > 0) chart.timeScale().setVisibleLogicalRange({ from, to: total + 4 });
      else chart.timeScale().fitContent();
      chart.subscribeCrosshairMove((p) => {
        setHovered(p.time ? String(p.time) : null);
        const id = typeof p.hoveredObjectId === "string" ? p.hoveredObjectId : null;
        setHoveredObject(id);
        hoverCallback.current?.(id);
      });
      live.current = { lc, chart, price, oscillator: null };
      setReady((n) => n + 1);
      cleanup = () => {
        live.current = null;
        chart.remove();
      };
    });
    return () => {
      disposed = true;
      cleanup();
    };
  }, [model]);

  // Layers change without rebuilding the chart.
  useEffect(() => {
    const l = live.current;
    if (!l) return;
    l.price.setObjects(drawn, null);
    if (l.oscillator) {
      for (const c of l.oscillator.carriers) l.chart.removeSeries(c);
      if (l.chart.panes().length > 2) l.chart.removePane(2);
      l.oscillator = null;
    }
    const oscillatorObjects = drawn.filter((d) => d.primitives.some((p) => p.kind === "path" && p.pane === "oscillator"));
    const scaled = oscillatorObjects.filter((o) => o.layer === "oscillator");
    if (!scaled.length) return;
    // Invisible carriers give the pane its price scale; the overlay draws the lines.
    const carriers = scaled.map((o) => {
      const series = l.chart.addSeries(
        l.lc.LineSeries,
        { lineVisible: false, crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false },
        2,
      );
      const seen = new Set<string>();
      const points: { time: LC.Time; value: number }[] = [];
      for (const p of o.primitives) {
        if (p.kind !== "path") continue;
        for (const c of p.points) {
          if (seen.has(c.date)) continue;
          seen.add(c.date);
          points.push({ time: c.date as LC.Time, value: c.value });
        }
      }
      series.setData(points);
      return series;
    });
    const overlay = new Overlay("oscillator", roleStyles(), FONT);
    carriers[0]!.attachPrimitive(overlay);
    overlay.setObjects(oscillatorObjects, null);
    l.chart.panes()[2]?.setHeight(120);
    l.oscillator = { overlay, carriers };
  }, [drawn, ready]);

  // Highlighting only redraws.
  useEffect(() => {
    const l = live.current;
    if (!l) return;
    const active = highlighted ?? hoveredObject;
    l.price.highlight(active);
    l.oscillator?.overlay.highlight(active);
  }, [highlighted, hoveredObject, drawn, ready]);

  const shown = (hovered && model.barsByTime.get(hovered)) || bars.at(-1);
  const object = drawn.find((d) => d.id === (hoveredObject ?? highlighted));

  return (
    <figure className="rounded-lg border border-line bg-surface">
      <figcaption className="flex flex-wrap items-baseline gap-x-5 gap-y-1 border-b border-line px-4 py-2.5 text-sm">
        {shown ? <Legend bar={shown} /> : <span className="text-muted">No weekly bars.</span>}
      </figcaption>
      <div
        className="flex min-h-9 flex-wrap items-baseline gap-x-4 gap-y-1 border-b border-line px-4 py-2 text-xs"
        data-testid="object-legend"
      >
        {object ? (
          <>
            <span className="font-semibold text-ink">{object.title}</span>
            {object.status && <span className="text-ink">{object.status.toLowerCase().replaceAll("_", " ")}</span>}
            {object.details.map(([k, v]) => (
              <span key={k} className="text-muted">
                {k} <span className="num text-ink">{v}</span>
              </span>
            ))}
            {object.provisional && <span className="text-warn">provisional</span>}
          </>
        ) : (
          <span className="text-muted">Point at a drawn object to see its stored facts.</span>
        )}
      </div>
      <div ref={container} className="h-[560px] w-full" data-testid="weekly-chart" />
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
      or other special session. Hatched band: a continuity break; no price movement is implied across it. Analysis is
      drawn over the current segment only, at the dates and prices the engine stored; triangles along the bottom mark
      when something became known; dashed or hollow means provisional.
    </p>
  );
}
