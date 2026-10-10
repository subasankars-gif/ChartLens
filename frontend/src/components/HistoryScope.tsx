"use client";

/**
 * What history this chart covers, in three distinct parts (Issue 1 review):
 *
 * * Analysed: the current continuity segment, from `usable_from`. Every analytical
 *   object on the page comes from it (K2, M3).
 * * Earlier, not analysed: sessions of earlier segments. They are kept and can be
 *   shown as bars, but no analysis crosses a continuity break.
 * * In view: whatever the chart's range buttons or drag/scroll show. A view only scrolls
 *   the chart; it never changes what is analysed.
 *
 * Everything shown is a stored fact of the security (its segments and dates) or a count
 * of the bars the API returned. Nothing analytical is computed here.
 */

import type { SecurityDetail, WeeklyBar } from "@/lib/api";
import { causeLabel, formatDate } from "@/lib/format";

export function HistoryScope({
  detail,
  bars,
  showEarlier,
  onShowEarlier,
}: {
  detail: SecurityDetail;
  bars: readonly WeeklyBar[] | null;
  showEarlier: boolean;
  onShowEarlier: (show: boolean) => void;
}) {
  const current = detail.segments.find((s) => s.continuity_segment_id === detail.current_segment_id);
  const earlier = detail.segments.filter((s) => s.continuity_segment_id !== detail.current_segment_id);
  const earlierSessions = earlier.reduce((n, s) => n + s.sessions, 0);
  const analysedBars = bars?.filter((b) => b.continuity_segment_id === detail.current_segment_id).length ?? null;

  return (
    <section
      aria-label="History on this chart"
      className="rounded-lg border border-line bg-surface px-4 py-3 text-sm"
      data-testid="history-scope"
    >
      <dl className="grid gap-x-4 gap-y-1.5 sm:grid-cols-[10rem_1fr]">
        <dt className="text-muted">Analysed</dt>
        <dd data-testid="history-analysed">
          {formatDate(detail.usable_from)} to {formatDate(detail.data_as_of)}, the current continuity segment
          {analysedBars !== null && (
            <>
              {" "}
              (<span className="num">{analysedBars}</span> weekly {analysedBars === 1 ? "bar" : "bars"})
            </>
          )}
          . Every analytical object on this page comes from this period only.
        </dd>

        <dt className="text-muted">Earlier, not analysed</dt>
        <dd className="flex flex-col gap-1" data-testid="history-earlier">
          {earlier.length === 0 || !current ? (
            <span>None. This security has one continuity segment, so its whole history is analysed.</span>
          ) : (
            <>
              <span data-testid="history-notice">
                <span className="num">{earlierSessions}</span> {earlierSessions === 1 ? "session" : "sessions"} from{" "}
                {formatDate(earlier[0]!.segment_start)} to {formatDate(earlier.at(-1)!.segment_end)}, before the
                continuity break on {formatDate(current.segment_start)} ({causeLabel(current.cause)}). They are kept as
                weekly bars without analysis; no analysis crosses the break.
              </span>
              <label className="flex items-center gap-2 self-start">
                <input
                  type="checkbox"
                  checked={showEarlier}
                  onChange={(e) => onShowEarlier(e.target.checked)}
                  data-testid="earlier-history"
                />
                Show earlier history on the chart ({earlier.length} earlier{" "}
                {earlier.length === 1 ? "segment" : "segments"}, bars only)
              </label>
            </>
          )}
        </dd>

        <dt className="text-muted">In view</dt>
        <dd data-testid="history-view">
          Chosen with the range buttons above the chart, or by dragging and scrolling it. The view changes only what is
          shown, never what is analysed.
        </dd>
      </dl>
    </section>
  );
}
