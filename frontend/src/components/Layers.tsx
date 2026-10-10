"use client";

/**
 * Layer toggles and the analysis panel (ADR-0023, ADR-0027). Toggles choose among
 * authoritative choices only: stored series, stored swing methods and sensitivities, and
 * the engine's own lists ("current") or every stored object ("all"). The panel lists the
 * drawn objects in the order the engine stored them, with their stored facts; definition
 * fit is shown with its components and never orders anything. Objects the chart refused
 * to place are counted and listed, never hidden.
 */

import type { ChartDocument } from "@/lib/layers/document";
import { words } from "@/lib/layers/display";
import { OSCILLATORS, averageNames } from "@/lib/layers/indicators";
import { LAYERS, type LayerRun, type LayerSettings } from "@/lib/layers/registry";
import { swingChoices } from "@/lib/layers/structure";
import { REFUSAL_TEXT, type Drawn, type LayerId } from "@/lib/layers/types";

export function LayerControls({
  settings,
  onChange,
  doc,
}: {
  settings: LayerSettings;
  onChange: (next: LayerSettings) => void;
  doc: ChartDocument;
}) {
  const toggle = (id: LayerId, on: boolean) => {
    const enabled = new Set(settings.enabled);
    if (on) enabled.add(id);
    else enabled.delete(id);
    onChange({ ...settings, enabled });
  };
  const choices = doc.swings ? swingChoices(doc.swings) : [];
  const averages = doc.indicators ? averageNames(doc.indicators) : [];
  return (
    <fieldset className="flex flex-col gap-2 rounded-lg border border-line bg-surface px-4 py-3" data-testid="layer-controls">
      <legend className="px-1 text-sm font-semibold">Layers</legend>
      <div className="flex flex-wrap gap-x-4 gap-y-2 text-sm">
        {LAYERS.map((l) => (
          <label key={l.id} className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={settings.enabled.has(l.id)}
              onChange={(e) => toggle(l.id, e.target.checked)}
              data-testid={`layer-${l.id}`}
            />
            {l.label}
          </label>
        ))}
      </div>
      <div className="flex flex-wrap gap-x-5 gap-y-2 text-xs text-muted">
        {settings.enabled.has("averages") && averages.length > 0 && (
          <span className="flex flex-wrap items-center gap-2">
            Averages
            {averages.map((name) => (
              <label key={name} className="flex items-center gap-1">
                <input
                  type="checkbox"
                  checked={settings.averages.includes(name)}
                  onChange={(e) =>
                    onChange({
                      ...settings,
                      averages: e.target.checked
                        ? averages.filter((n) => n === name || settings.averages.includes(n))
                        : settings.averages.filter((n) => n !== name),
                    })
                  }
                />
                <span className="num">{name}</span>
              </label>
            ))}
          </span>
        )}
        {settings.enabled.has("oscillator") && (
          <Choice
            label="Oscillator"
            value={settings.oscillator}
            options={Object.keys(OSCILLATORS).map((k) => [k, k.toUpperCase()])}
            onChange={(v) => onChange({ ...settings, oscillator: v as LayerSettings["oscillator"] })}
          />
        )}
        {settings.enabled.has("swings") && doc.swings && (
          <Choice
            label="Swings"
            value={settings.swings === "primary" ? "primary" : `${settings.swings.method}|${settings.swings.sensitivity}`}
            options={[
              ["primary", `Primary (${doc.swings.primary_method}, ${words(doc.swings.primary_sensitivity)})`],
              ...choices.map((c) => [`${c.method}|${c.sensitivity}`, `${c.method}, ${words(c.sensitivity)}`] as const),
            ]}
            onChange={(v) => {
              const [method, sensitivity] = v.split("|");
              onChange({ ...settings, swings: v === "primary" ? "primary" : { method: method!, sensitivity: sensitivity! } });
            }}
            testId="swing-selection"
          />
        )}
        {settings.enabled.has("fibonacci") && (
          <Choice
            label="Fibonacci"
            value={settings.fibonacci}
            options={[
              ["current", "Current (engine list)"],
              ["all", "All stored"],
            ]}
            onChange={(v) => onChange({ ...settings, fibonacci: v as "current" | "all" })}
          />
        )}
        {settings.enabled.has("patterns") && (
          <Choice
            label="Patterns"
            value={settings.patterns}
            options={[
              ["current", "Included (engine list)"],
              ["all", "All stored"],
            ]}
            onChange={(v) => onChange({ ...settings, patterns: v as "current" | "all" })}
            testId="pattern-selection"
          />
        )}
        {settings.enabled.has("breakouts") && (
          <Choice
            label="Breakouts of"
            value={settings.breakouts}
            options={[
              ["pattern", "Patterns"],
              ["level", "Levels"],
            ]}
            onChange={(v) => onChange({ ...settings, breakouts: v as "pattern" | "level" })}
          />
        )}
      </div>
    </fieldset>
  );
}

function Choice({
  label,
  value,
  options,
  onChange,
  testId,
}: {
  label: string;
  value: string;
  options: readonly (readonly [string, string])[];
  onChange: (v: string) => void;
  testId?: string;
}) {
  return (
    <label className="flex items-center gap-1.5">
      {label}
      <select
        className="rounded border border-line bg-surface px-1.5 py-0.5 text-ink"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        data-testid={testId}
      >
        {options.map(([v, text]) => (
          <option key={v} value={v}>
            {text}
          </option>
        ))}
      </select>
    </label>
  );
}

export function AnalysisPanel({
  run,
  doc,
  focused,
  onFocus,
}: {
  run: LayerRun;
  doc: ChartDocument;
  focused: string | null;
  onFocus: (id: string | null) => void;
}) {
  const listed = run.results.filter((r) => !["averages", "bollinger", "oscillator"].includes(r.layer));
  const refused = run.results.flatMap((r) => r.refused);
  const drawnCount = run.results.reduce((n, r) => n + r.drawn.length, 0);
  return (
    <section className="flex flex-col gap-3 rounded-lg border border-line bg-surface p-4" aria-label="Drawn analysis">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold">Drawn analysis</h2>
        <p className={refused.length ? "text-sm text-warn" : "text-xs text-muted"} data-testid="unplaced">
          {refused.length
            ? `${refused.length} ${refused.length === 1 ? "object" : "objects"} not drawn (listed below)`
            : `${drawnCount} ${drawnCount === 1 ? "object" : "objects"} drawn at their stored dates and prices; 0 not drawn`}
        </p>
      </div>
      {listed.length === 0 && <p className="text-sm text-muted">Turn on a layer to draw the stored analysis.</p>}
      {listed.map((r) => (
        <details key={r.layer} open={r.drawn.length <= 12} className="text-sm" data-testid={`panel-${r.layer}`}>
          <summary className="cursor-pointer select-none font-medium">
            {LAYERS.find((l) => l.id === r.layer)?.label} <span className="num text-muted">({r.drawn.length})</span>
          </summary>
          <ul className="mt-1 flex flex-col divide-y divide-line">
            {r.drawn.map((d) => (
              <PanelItem key={d.id} object={d} doc={doc} focused={focused === d.id} onFocus={onFocus} />
            ))}
          </ul>
        </details>
      ))}
      {refused.length > 0 && (
        <div className="rounded border border-warn/40 p-2 text-xs" data-testid="unplaced-list">
          <p className="mb-1 font-medium text-warn">Not drawn: a chart object is never moved to a nearby bar.</p>
          <ul className="flex flex-col gap-0.5">
            {refused.map((x) => (
              <li key={`${x.layer}-${x.id}`}>
                <span className="font-mono">{x.id}</span>: {REFUSAL_TEXT[x.reason]} ({x.detail})
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function PanelItem({
  object,
  doc,
  focused,
  onFocus,
}: {
  object: Drawn;
  doc: ChartDocument;
  focused: boolean;
  onFocus: (id: string | null) => void;
}) {
  const pattern = object.layer === "patterns" ? doc.patterns?.patterns.find((p) => p.pattern_id === object.id) : undefined;
  return (
    <li className={`py-1.5 ${focused ? "bg-canvas" : ""}`} data-testid="panel-item">
      <button
        type="button"
        className="flex w-full flex-wrap items-baseline gap-x-3 text-left"
        onClick={() => onFocus(focused ? null : object.id)}
        aria-pressed={focused}
      >
        <span className="font-medium">{object.title}</span>
        {object.status && <span data-testid="panel-status">{words(object.status)}</span>}
        <span className="text-muted">known {object.knownAt}</span>
        {object.provisional && <span className="text-warn">provisional</span>}
      </button>
      {focused && (
        <dl className="mt-1 grid grid-cols-[max-content_1fr] gap-x-3 text-xs">
          {object.details.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-muted">{k}</dt>
              <dd className="num">{v}</dd>
            </div>
          ))}
          <dt className="text-muted">Id</dt>
          <dd className="font-mono break-all">{object.id}</dd>
        </dl>
      )}
      {focused && pattern && (
        <table className="mt-2 w-full text-xs" data-testid="definition-fit">
          <caption className="text-left text-muted">
            Definition fit {pattern.definition_fit.value}: how closely the stored geometry meets the pattern&apos;s definition
            (not a likelihood).
          </caption>
          <tbody className="num">
            {pattern.definition_fit.components.map((c) => (
              <tr key={c.component} className="border-t border-line">
                <td className="py-0.5 pr-2">{words(c.component)}</td>
                <td className="pr-2 text-muted">{words(c.aspect)}</td>
                <td className="pr-2 text-muted">{words(c.status)}</td>
                <td className="text-right">{c.score === null ? "—" : c.score.toFixed(4)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </li>
  );
}
