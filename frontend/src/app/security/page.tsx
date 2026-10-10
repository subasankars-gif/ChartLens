"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";
import { CurrentState } from "@/components/CurrentState";
import { HistoryScope } from "@/components/HistoryScope";
import { AnalysisPanel, LayerControls } from "@/components/Layers";
import { WeeklyChart } from "@/components/WeeklyChart";
import { ApiError, api, type DataQuality, type SecurityDetail } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useChartData, useRecentBreakouts } from "@/lib/chart-data";
import { causeLabel, formatDate } from "@/lib/format";
import { frameOf } from "@/lib/layers/place";
import { DEFAULT_SETTINGS, runLayers, type LayerSettings } from "@/lib/layers/registry";

export default function SecurityPage() {
  return (
    <Suspense fallback={<p className="text-sm text-muted">Loading…</p>}>
      <Security />
    </Suspense>
  );
}

type Loaded = { detail: SecurityDetail; quality: DataQuality };

function Security() {
  const id = useSearchParams().get("id") ?? "";
  const { token } = useAuth();
  const [loadedState, setLoaded] = useState<(Loaded & { id: string }) | null>(null);
  const [allSegments, setAllSegments] = useState(false);
  const [settings, setSettings] = useState<LayerSettings>(DEFAULT_SETTINGS);
  const [focused, setFocused] = useState<string | null>(null);
  const [errorState, setError] = useState<{ key: string; message: string } | null>(null);
  const loaded = loadedState?.id === id ? loadedState : null;
  const chartData = useChartData(token, id, allSegments ? "all" : "valid", settings);
  const chart = chartData.loaded?.chart ?? null;
  const weekly = chart?.weekly ?? null;
  const error = (errorState && errorState.key === id ? errorState.message : null) ?? chartData.error;
  const doc = chartData.loaded?.doc;
  const run = useMemo(
    () => (weekly && doc ? runLayers(frameOf(weekly), doc, settings, chartData.breakoutRows) : null),
    [weekly, doc, settings, chartData.breakoutRows],
  );
  const drawn = useMemo(() => run?.results.flatMap((r) => r.drawn) ?? [], [run]);
  const recent = useRecentBreakouts(token, id, chart, doc?.current?.trend_since ?? null);
  const today = useMemo(() => new Date().toLocaleDateString("en-CA"), []);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    Promise.all([api.security(token, id), api.dataQuality(token, id)])
      .then(([detail, quality]) => !cancelled && setLoaded({ id, detail, quality }))
      .catch(
        (err: unknown) =>
          !cancelled &&
          setError({
            key: id,
            message:
              err instanceof ApiError && err.kind === "not_found"
                ? "No security with this id in the current data."
                : err instanceof Error
                  ? err.message
                  : String(err),
          }),
      );
    return () => {
      cancelled = true;
    };
  }, [id, token]);

  const causes = useMemo(
    () => new Map((loaded?.detail.segments ?? []).map((s) => [s.continuity_segment_id, causeLabel(s.cause)])),
    [loaded],
  );

  if (!id) return <p className="text-sm text-muted">Choose a security with the search box.</p>;
  if (error)
    return (
      <p role="alert" className="text-sm text-down">
        {error}
      </p>
    );
  if (!loaded) return <p className="text-sm text-muted">Loading…</p>;
  const { detail, quality } = loaded;
  const current = detail.segments.find((s) => s.continuity_segment_id === detail.current_segment_id);

  return (
    <article className="flex flex-col gap-6">
      <header className="flex flex-col gap-2">
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
          <h1 className="text-3xl font-semibold tracking-tight" data-testid="symbol">
            {detail.symbol ?? detail.isin ?? detail.security_id}
          </h1>
          <p className="text-lg text-muted">{detail.name}</p>
        </div>
        <dl className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
          <Fact term="ISIN" value={detail.isin ?? "—"} />
          <Fact term="Status" value={statusLabel(detail.status)} warn={detail.status !== "USABLE"} />
          <Fact term="Usable from" value={formatDate(detail.usable_from)} testId="usable-from" />
          {current && current.cause !== "FIRST_SESSION" && <Fact term="Because of" value={causeLabel(current.cause)} />}
          <Fact term="Listing" value={detail.listing_status.toLowerCase()} />
          <Fact term="Data through" value={formatDate(detail.data_as_of)} />
        </dl>
        {!detail.analytical_universe && (
          <p className="text-sm text-warn">
            This instrument ({detail.instrument_type.toLowerCase().replaceAll("_", " ")}) is kept in the data but is
            not part of the analysed universe.
          </p>
        )}
      </header>

      {chart && chart.analysis_status === "analysed" && doc && (
        <CurrentState
          chart={chart}
          doc={doc}
          lastBar={weekly?.bars.at(-1) ?? null}
          today={today}
          breakouts={recent}
          focused={focused}
          onFocus={(target, layer) => {
            setFocused(target);
            if (layer && !settings.enabled.has(layer)) {
              setSettings({ ...settings, enabled: new Set([...settings.enabled, layer]) });
            }
          }}
        />
      )}

      <section aria-label="Weekly chart" className="flex flex-col gap-2">
        <HistoryScope
          detail={detail}
          bars={weekly?.bars ?? null}
          showEarlier={allSegments}
          onShowEarlier={setAllSegments}
        />
        {chart && chart.analysis_status === "analysed" && doc ? (
          <LayerControls settings={settings} onChange={setSettings} doc={doc} />
        ) : chart ? (
          <p className="text-sm text-muted" data-testid="analysis-status">
            {chart.analysis_status === "not_analysed"
              ? "This security is not in the analysed universe: the chart shows its weekly bars only."
              : "The current data snapshot carries no analysis yet: the chart shows weekly bars only."}
          </p>
        ) : null}
        {weekly ? (
          <WeeklyChart
            bars={weekly.bars}
            currentSegmentId={weekly.current_segment_id}
            causes={causes}
            drawn={drawn}
            highlighted={focused}
          />
        ) : (
          <div className="h-[660px] rounded-lg border border-line bg-surface" />
        )}
        {run && doc && <AnalysisPanel run={run} doc={doc} focused={focused} onFocus={setFocused} />}
      </section>

      <div className="grid gap-6 lg:grid-cols-2">
        <Panel title="Continuity segments">
          <table className="w-full text-sm">
            <thead className="text-left text-muted">
              <tr>
                <th className="py-1 font-normal">From</th>
                <th className="py-1 font-normal">To</th>
                <th className="py-1 text-right font-normal">Sessions</th>
                <th className="py-1 pl-4 font-normal">Starts with</th>
              </tr>
            </thead>
            <tbody className="num">
              {detail.segments.map((s) => (
                <tr key={s.continuity_segment_id} className="border-t border-line">
                  <td className="py-1.5">{formatDate(s.segment_start)}</td>
                  <td className="py-1.5">{formatDate(s.segment_end)}</td>
                  <td className="py-1.5 text-right">{s.sessions}</td>
                  <td className="py-1.5 pl-4">
                    {causeLabel(s.cause)}
                    {s.continuity_segment_id === detail.current_segment_id && (
                      <span className="ml-2 text-xs text-accent">valid now</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>

        <Panel title="Symbol and ISIN history">
          <table className="w-full text-sm">
            <tbody className="num">
              {detail.identifiers
                .filter((i) => i.identifier_type === "SYMBOL" || i.identifier_type === "ISIN")
                .map((i) => (
                  <tr key={`${i.identifier_type}-${i.identifier_value}-${i.valid_from}`} className="border-t border-line first:border-0">
                    <td className="py-1.5 pr-3 text-muted">{i.identifier_type === "ISIN" ? "ISIN" : "Symbol"}</td>
                    <td className="py-1.5 pr-3 font-medium">{i.identifier_value}</td>
                    <td className="py-1.5 text-muted">
                      {formatDate(i.valid_from)} to {formatDate(i.valid_to)}
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </Panel>

        <Panel title={`Data quality findings (${quality.findings.length})`}>
          {quality.findings.length === 0 ? (
            <p className="text-sm text-muted">No findings.</p>
          ) : (
            <ul className="flex flex-col divide-y divide-line text-sm" data-testid="findings">
              {quality.findings.map((f, i) => (
                <li key={`${f.code}-${f.start_date}-${i}`} className="py-2">
                  <p>
                    <span className={f.severity === "INFO" ? "text-muted" : "text-warn"}>{severityLabel(f.severity)}</span>{" "}
                    <span className="font-medium">{f.code.toLowerCase().replaceAll("_", " ")}</span>
                    {f.start_date && <span className="text-muted">, {formatDate(f.start_date)}</span>}
                    {f.breaks_continuity && <span className="ml-2 text-xs text-break">breaks continuity</span>}
                  </p>
                  <p className="text-muted">{f.detail}</p>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="Provenance">
          <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm" data-testid="provenance">
            <dt className="text-muted">Security id</dt>
            <dd className="font-mono text-xs leading-5">{detail.security_id}</dd>
            <dt className="text-muted">Serving snapshot</dt>
            <dd className="font-mono text-xs leading-5">{chart?.meta_version ?? detail.meta_version}</dd>
            {chart?.analysis && (
              <>
                <dt className="text-muted">Analysis</dt>
                <dd className="font-mono text-xs leading-5">{chart.analysis.envelope.analysis_version}</dd>
                <dt className="text-muted">Analysis document</dt>
                <dd className="font-mono text-xs leading-5 break-all">{chart.analysis.envelope.document_sha256}</dd>
              </>
            )}
            {Object.entries(weekly?.versions ?? {})
              .sort(([a], [b]) => a.localeCompare(b))
              .map(([k, v]) => (
                <Version key={k} name={k} value={v} />
              ))}
            <dt className="text-muted">Data through</dt>
            <dd>{formatDate(detail.data_as_of)}</dd>
          </dl>
        </Panel>
      </div>
    </article>
  );
}

function Version({ name, value }: { name: string; value: string }) {
  const label = name.replace(/_version$/, "").replace("_", " ").replace("dq", "data quality");
  return (
    <>
      <dt className="text-muted">{label.charAt(0).toUpperCase() + label.slice(1)}</dt>
      <dd className="font-mono text-xs leading-5">{value}</dd>
    </>
  );
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-line bg-surface p-4">
      <h2 className="mb-2 text-sm font-semibold">{title}</h2>
      {children}
    </section>
  );
}

function Fact({ term, value, warn, testId }: { term: string; value: string; warn?: boolean; testId?: string }) {
  return (
    <div className="flex gap-1.5">
      <dt className="text-muted">{term}</dt>
      <dd className={warn ? "text-warn" : undefined} data-testid={testId}>
        {value}
      </dd>
    </div>
  );
}

function statusLabel(status: string): string {
  return ({ USABLE: "Usable", USABLE_WITH_WARNINGS: "Usable, with warnings", NOT_USABLE: "Not usable" } as Record<
    string,
    string
  >)[status] ?? status;
}

function severityLabel(s: string): string {
  return ({ INFO: "Note:", WARN: "Warning:", FAIL: "Failure:" } as Record<string, string>)[s] ?? s;
}
