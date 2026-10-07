"use client";

import type { RunView, StageRecord } from "@/lib/api";
import { STAGE_ORDER, formatDuration, formatInstant, stageLabel, statusLabel } from "@/lib/operations";

/**
 * A production run's stages, in order, exactly as the run record has them (ADR-0018).
 * Records written before ANALYSIS existed have six stages and are drawn with six; nothing
 * is invented for a stage the run never had. The strip is the run at a glance; the table
 * under it is the evidence.
 */

const SEGMENT: Record<StageRecord["status"], string> = {
  SUCCEEDED: "bg-accent",
  RUNNING: "bg-accent/45 run-stripes",
  FAILED: "bg-down",
  CANCELLED: "bg-line opacity-60",
  QUEUED: "bg-line",
};

export function StageStrip({ run }: { run: RunView }) {
  const byStage = new Map(run.stages.map((s) => [s.stage, s]));
  const names = STAGE_ORDER.filter((name) => byStage.has(name));
  const shown = names.length > 0 ? names : STAGE_ORDER;
  return (
    <ol
      className="grid gap-1"
      style={{ gridTemplateColumns: `repeat(${shown.length}, minmax(0, 1fr))` }}
      aria-label="Stages"
      data-testid="stage-strip"
    >
      {shown.map((name) => {
        const stage = byStage.get(name);
        const status = stage?.status ?? "QUEUED";
        return (
          <li key={name} className="min-w-0" data-stage={name} data-status={status}>
            <div className={`h-2 rounded-sm ${SEGMENT[status]}`} />
            <p className="mt-1.5 truncate text-xs font-medium">{stageLabel(name)}</p>
            <p className={`truncate text-xs ${status === "FAILED" ? "text-down" : "text-muted"}`}>
              {status === "SUCCEEDED" ? formatDuration(stage?.duration_seconds) : statusLabel(status)}
            </p>
          </li>
        );
      })}
    </ol>
  );
}

export function StageTable({ run }: { run: RunView }) {
  return (
    <table className="mt-4 w-full text-sm" data-testid="stage-table">
      <thead className="text-left text-muted">
        <tr>
          <th className="py-1 font-normal">Stage</th>
          <th className="py-1 font-normal">Status</th>
          <th className="py-1 font-normal">Started</th>
          <th className="py-1 text-right font-normal">Took</th>
          <th className="hidden py-1 text-right font-normal sm:table-cell">Records</th>
          <th className="hidden py-1 pl-4 font-normal md:table-cell">Version</th>
        </tr>
      </thead>
      <tbody>
        {run.stages.map((s) => (
          <tr key={s.stage} className="border-t border-line align-top">
            <td className="py-2">{stageLabel(s.stage)}</td>
            <td className={`py-2 ${s.status === "FAILED" ? "text-down" : ""}`}>
              {statusLabel(s.status)}
              {s.error_summary && <p className="text-xs text-down">{s.error_summary}</p>}
            </td>
            <td className="py-2 text-muted">{formatInstant(s.started_at)}</td>
            <td className="num py-2 text-right">{formatDuration(s.duration_seconds)}</td>
            <td className="num hidden py-2 text-right sm:table-cell">
              {s.records_processed === null || s.records_processed === undefined
                ? "—"
                : s.records_processed.toLocaleString("en-IN")}
            </td>
            <td className="hidden py-2 pl-4 font-mono text-xs break-all text-muted md:table-cell">
              {s.version ?? "—"}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function RunStages({ run }: { run: RunView }) {
  return (
    <>
      <StageStrip run={run} />
      <StageTable run={run} />
    </>
  );
}
