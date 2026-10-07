/**
 * Loading a chart from one snapshot (ADR-0027 §3, §6). The chart comes from `/chart`
 * (bars and the small sections together); heavier sections and breakout events are
 * fetched when their layer is turned on and accepted only when they name the chart's
 * own `meta_version`. Anything from another snapshot makes the whole chart reload: a
 * chart that never existed is never shown.
 */

import { useEffect, useMemo, useState } from "react";
import { api, type ChartResponse, type TokenSource } from "./api";
import type { BreakoutRow, ChartDocument } from "./layers/document";
import { BASE_SECTIONS, sectionsFor, type LayerSettings } from "./layers/registry";

export type LoadedChart = {
  key: string;
  chart: ChartResponse;
  doc: ChartDocument;
  sections: ReadonlySet<string>;
};

export class SnapshotChanged extends Error {
  constructor(readonly expected: string, readonly received: string) {
    super(`Data from snapshot ${received} arrived for a chart of snapshot ${expected}.`);
  }
}

/** The single consistency rule: a component is accepted only from the chart's snapshot. */
export function acceptFrom(chartMeta: string, componentMeta: string): void {
  if (componentMeta !== chartMeta) throw new SnapshotChanged(chartMeta, componentMeta);
}

/** Check a `/chart` response's own components agree (they always should). */
export function checkChart(chart: ChartResponse): void {
  acceptFrom(chart.meta_version, chart.weekly.meta_version);
  if (chart.analysis) acceptFrom(chart.meta_version, chart.analysis.envelope.meta_version);
}

export function useChartData(token: TokenSource, id: string, segments: "valid" | "all", settings: LayerSettings) {
  const key = `${id}|${segments}`;
  const wanted = useMemo(() => sectionsFor(settings), [settings]);
  const [loaded, setLoaded] = useState<LoadedChart | null>(null);
  const [rows, setRows] = useState<{ key: string; meta: string; source: string; rows: BreakoutRow[] } | null>(null);
  const [error, setError] = useState<{ key: string; message: string } | null>(null);
  const [reloads, setReloads] = useState(0);

  const current = loaded?.key === key ? loaded : null;

  // The chart itself: bars plus the base sections and those the enabled layers need.
  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    const sections = [...new Set<string>([...BASE_SECTIONS, ...wanted])];
    api
      .chart(token, id, segments, sections)
      .then((chart) => {
        if (cancelled) return;
        checkChart(chart);
        setLoaded({
          key,
          chart,
          doc: (chart.analysis?.document ?? {}) as ChartDocument,
          sections: new Set(chart.analysis ? sections : []),
        });
      })
      .catch((err: unknown) => !cancelled && setError({ key, message: err instanceof Error ? err.message : String(err) }));
    return () => {
      cancelled = true;
    };
    // `wanted` is read once per chart load; later additions are fetched below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, token, segments, key, reloads]);

  // Further sections, as layers are turned on.
  useEffect(() => {
    if (!current || current.chart.analysis_status !== "analysed") return;
    const missing = wanted.filter((s) => !current.sections.has(s));
    if (!missing.length) return;
    let cancelled = false;
    api
      .analysis(token, id, missing)
      .then((response) => {
        if (cancelled) return;
        try {
          acceptFrom(current.chart.meta_version, response.envelope.meta_version);
        } catch {
          setReloads((n) => n + 1); // another snapshot: reload the whole chart
          return;
        }
        setLoaded({
          ...current,
          doc: { ...current.doc, ...(response.document as ChartDocument) },
          sections: new Set([...current.sections, ...missing]),
        });
      })
      .catch((err: unknown) => !cancelled && setError({ key, message: err instanceof Error ? err.message : String(err) }));
    return () => {
      cancelled = true;
    };
  }, [current, wanted, token, id, key]);

  // Breakout events, one stored dataset at a time.
  const wantRows = settings.enabled.has("breakouts");
  const source = settings.breakouts;
  useEffect(() => {
    if (!current || !wantRows || current.chart.analysis_status !== "analysed") return;
    if (rows && rows.key === key && rows.meta === current.chart.meta_version && rows.source === source) return;
    let cancelled = false;
    (async () => {
      const all: BreakoutRow[] = [];
      let cursor: string | undefined;
      do {
        const page = await api.breakoutEvents(token, id, source, cursor);
        acceptFrom(current.chart.meta_version, page.envelope.meta_version);
        all.push(...(page.rows as unknown as BreakoutRow[]));
        cursor = page.next_cursor ?? undefined;
      } while (cursor && !cancelled);
      if (!cancelled) setRows({ key, meta: current.chart.meta_version, source, rows: all });
    })().catch((err: unknown) => {
      if (cancelled) return;
      if (err instanceof SnapshotChanged) setReloads((n) => n + 1);
      else setError({ key, message: err instanceof Error ? err.message : String(err) });
    });
    return () => {
      cancelled = true;
    };
  }, [current, wantRows, source, rows, token, id, key]);

  const breakoutRows =
    current && rows && rows.key === key && rows.meta === current.chart.meta_version && rows.source === source
      ? rows.rows
      : null;
  return {
    loaded: current,
    breakoutRows,
    error: error?.key === key ? error.message : null,
  };
}
