/**
 * Display helpers for production runs (ADR-0018). Everything shown comes from the API's
 * run records; these only turn codes and timestamps into words.
 */

import type { RunView } from "./api";

export type RunStatus = RunView["status"];
export type StageName = NonNullable<RunView["current_stage"]>;

export const STAGE_ORDER: readonly StageName[] = [
  "INGEST",
  "CORPORATE_ACTIONS",
  "ADJUSTMENT",
  "DATA_QUALITY",
  "WEEKLY",
  "ANALYSIS",
  "PUBLISH_SERVING",
];

const STAGE_LABEL: Record<StageName, string> = {
  INGEST: "Ingest",
  CORPORATE_ACTIONS: "Corporate actions",
  ADJUSTMENT: "Adjustment",
  DATA_QUALITY: "Data quality",
  WEEKLY: "Weekly bars",
  ANALYSIS: "Analysis",
  PUBLISH_SERVING: "Publish",
};

const STATUS_LABEL: Record<RunStatus, string> = {
  QUEUED: "Queued",
  RUNNING: "Running",
  SUCCEEDED: "Succeeded",
  FAILED: "Failed",
  CANCELLED: "Cancelled",
};

export function stageLabel(stage: StageName | null | undefined): string {
  return stage ? STAGE_LABEL[stage] : "—";
}

export function statusLabel(status: RunStatus): string {
  return STATUS_LABEL[status];
}

export function isActive(status: RunStatus): boolean {
  return status === "QUEUED" || status === "RUNNING";
}

/** What the run did to the live snapshot, in words. */
export function outcomeLabel(run: Pick<RunView, "status" | "snapshot_outcome">): string {
  if (isActive(run.status)) return "In progress; the live snapshot is unchanged until it publishes";
  switch (run.snapshot_outcome) {
    case "PUBLISHED":
      return "Published a new snapshot";
    case "UNCHANGED":
      return "No new data; the live snapshot was already current";
    default:
      return "Not published; the live snapshot is unchanged";
  }
}

/** "schedule" → "Schedule"; "github:octocat" → "octocat on GitHub"; emails as given. */
export function requesterLabel(run: Pick<RunView, "trigger" | "requested_by">): string {
  if (run.trigger === "schedule") return "Schedule";
  if (run.requested_by.startsWith("github:")) return `${run.requested_by.slice(7)} on GitHub`;
  return run.requested_by;
}

/** 42 → "42 s"; 200 → "3 min 20 s"; 3900 → "1 h 5 min". */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return s % 60 ? `${m} min ${s % 60} s` : `${m} min`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
}

const IST = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata",
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

/** An instant in Indian time, the market's clock: "2 Oct, 20:15 IST". */
export function formatInstant(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${IST.format(d)} IST`;
}

/** Elapsed time of an active run, from its start (or request) to now. */
export function elapsedSeconds(run: Pick<RunView, "started_at" | "requested_at">, now: number): number {
  return (now - new Date(run.started_at ?? run.requested_at).getTime()) / 1000;
}
