"use client";

/**
 * "Current weekly state": what the published analysis says the chart shows now, above the
 * chart (Issue 2). It selects and restates stored objects of the chart's own snapshot and
 * computes nothing (ADR-0027, ADR-0028; `lib/current-state.ts` says exactly what is
 * selected and why).
 */

import type { ChartResponse, WeeklyBar } from "@/lib/api";
import { breakoutsSince, STALE_AFTER_DAYS, structureChangesSince } from "@/lib/current-state";
import type { RecentBreakouts } from "@/lib/chart-data";
import { ageInDays, formatDate } from "@/lib/format";
import { sentence, value4, words } from "@/lib/layers/display";
import type { BreakoutRow, ChartDocument, StructureEvent } from "@/lib/layers/document";
import type { LayerId } from "@/lib/layers/types";
import { ExplanationPanel } from "./Explanations";

/** At most this many events per list are written out; the rest are counted (presentation). */
const LIST_LIMIT = 12;

export function CurrentState({
  chart,
  doc,
  lastBar,
  today,
  breakouts,
  focused,
  onFocus,
}: {
  chart: ChartResponse;
  doc: ChartDocument;
  lastBar: WeeklyBar | null;
  today: string;
  breakouts: { rows: RecentBreakouts | null; error: string | null } | null;
  focused: string | null;
  onFocus: (id: string | null, layer: LayerId | null) => void;
}) {
  const current = doc.current;
  const since = current?.trend_since ?? null;
  const age = ageInDays(chart.data_as_of, today);
  const changes = structureChangesSince(doc, since);
  return (
    <section className="flex flex-col gap-3 rounded-lg border border-line bg-surface p-4" aria-label="Current weekly state" data-testid="current-state">
      <div className="flex flex-col gap-1">
        <h2 className="text-base font-semibold">Current weekly state</h2>
        <dl className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
          <div className="flex gap-1.5">
            <dt className="text-muted">As of the week ending</dt>
            <dd data-testid="state-date">{formatDate(current?.state_date)}</dd>
          </div>
          <div className="flex gap-1.5">
            <dt className="text-muted">Data through</dt>
            <dd data-testid="state-data-as-of">{formatDate(chart.data_as_of)}</dd>
          </div>
          <div className="flex gap-1.5">
            <dt className="text-muted">Snapshot</dt>
            <dd className="font-mono text-xs leading-5" data-testid="state-snapshot">
              {chart.meta_version}
            </dd>
          </div>
        </dl>
        {age > STALE_AFTER_DAYS && (
          <p className="text-sm text-warn" data-testid="state-stale">
            This data is {age} days old: the state below is as of {formatDate(chart.data_as_of)}, not today.
          </p>
        )}
        {lastBar && !lastBar.is_complete && (
          <p className="text-sm text-muted" data-testid="state-forming">
            The week from {formatDate(lastBar.first_session_date)} is still forming; it is drawn on the chart but is
            not part of this state.
          </p>
        )}
      </div>

      {chart.explanations ? (
        <ExplanationPanel explanation={chart.explanations} focused={focused} onFocus={onFocus} compact />
      ) : (
        <p className="text-sm text-muted">This snapshot carries no explanation of the current state.</p>
      )}

      <section className="flex flex-col gap-2 text-sm" aria-label="Changes since the current trend state began" data-testid="state-changes">
        <h3 className="font-semibold">
          {since ? `Changes since the current trend state began (${formatDate(since)})` : "Changes since the current trend state began"}
        </h3>
        {!since ? (
          <p className="text-muted">The analysis stores no current trend state, so there is no period to list.</p>
        ) : (
          <>
            <EventList
              title="Market-structure events"
              testId="state-structure-events"
              items={changes.map((e) => structureText(e))}
            />
            {breakouts?.error ? (
              <p className="text-warn">Breakout events could not be loaded: {breakouts.error}</p>
            ) : !breakouts?.rows ? (
              <p className="text-muted">Loading breakout events…</p>
            ) : (
              <>
                <EventList
                  title="Pattern breakout events"
                  testId="state-pattern-breakouts"
                  items={breakoutsSince(breakouts.rows.pattern, since).map((r) => breakoutText(r, r.pattern_type))}
                />
                <EventList
                  title="Level breakout events"
                  testId="state-level-breakouts"
                  items={breakoutsSince(breakouts.rows.level, since).map((r) => breakoutText(r, r.level_source_type))}
                />
              </>
            )}
          </>
        )}
      </section>

      <p className="text-xs text-muted" data-testid="state-limits">
        Not shown, because the stored analysis does not contain it: the price at which the current trend state would
        change. Patterns show only the confirmation and invalidation conditions the engine stored.
      </p>
    </section>
  );
}

function EventList({ title, testId, items }: { title: string; testId: string; items: string[] }) {
  const shown = items.slice(-LIST_LIMIT);
  return (
    <div data-testid={testId}>
      <p className="text-muted">
        {title} <span className="num">({items.length})</span>
      </p>
      {items.length === 0 ? (
        <p className="pl-3 text-muted">None.</p>
      ) : (
        <ul className="flex flex-col pl-3">
          {items.length > shown.length && (
            <li className="text-xs text-muted">{items.length - shown.length} earlier ones are on the chart&apos;s layers.</li>
          )}
          {shown.map((t, i) => (
            <li key={`${i}-${t}`} data-testid="state-event">
              {t}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function structureText(e: StructureEvent): string {
  return `${e.kind} ${words(e.direction)} on ${formatDate(e.bar_date)} at ${value4(e.level)}${e.provisional ? " (provisional)" : ""}`;
}

function breakoutText(r: BreakoutRow, type: string | undefined): string {
  const follow = r.history.map((h) => `${words(h.kind)} ${formatDate(h.effective_date)}`).join(", ");
  return `${sentence(r.direction)} of ${words(type ?? r.source_type)} on ${formatDate(r.bar_date)} at ${value4(
    r.level_at_break,
  )}${follow ? `; then ${follow}` : ""}${r.provisional ? " (provisional)" : ""}`;
}
