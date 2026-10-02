"use client";

/**
 * System: what the API is serving, the production refresh runs, and (for admins) the
 * refresh control (ADR-0018). The API authorizes every call. It refuses a refresh from
 * anyone but an admin, whatever this page shows. A refresh runs in GitHub Actions; this
 * page only polls its record, every 5 seconds while one is active.
 */

import { useCallback, useEffect, useState } from "react";
import { RunStages } from "@/components/RunStages";
import { ApiError, api, type OperationsStatus, type RunView, type SnapshotView } from "@/lib/api";
import { useAccess } from "@/lib/access";
import { useAuth } from "@/lib/auth";
import { formatDate } from "@/lib/format";
import {
  elapsedSeconds,
  formatDuration,
  formatInstant,
  isActive,
  outcomeLabel,
  requesterLabel,
  stageLabel,
  statusLabel,
} from "@/lib/operations";

const POLL_MS = 5000;

type Notice = { tone: "ok" | "error"; text: string };

export default function SystemPage() {
  const { token } = useAuth();
  const access = useAccess();
  const isAdmin = access.status === "ready" && access.user.role === "admin";
  const [ops, setOps] = useState<OperationsStatus | null>(null);
  const [runs, setRuns] = useState<RunView[] | null>(null);
  const [snapshots, setSnapshots] = useState<SnapshotView[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<RunView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(0);

  const load = useCallback(() => {
    Promise.all([api.operations(token), api.jobs(token, 20), api.snapshots(token, 10)])
      .then(([o, page, snaps]) => {
        setOps(o);
        setRuns(page.runs);
        setSnapshots(snaps);
        setError(null);
        setNow(Date.now());
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, [token]);
  useEffect(load, [load]);

  // The run shown in detail: the one picked, else the active one.
  const focus = selected ?? ops?.active_run?.run_id ?? null;
  const loadDetail = useCallback(() => {
    if (!focus) return;
    api
      .job(token, focus)
      .then(setDetail)
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, [token, focus]);
  useEffect(loadDetail, [loadDetail]);

  const shown = detail && detail.run_id === focus ? detail : null;
  const polling = Boolean(ops?.active_run) || (shown !== null && isActive(shown.status));
  useEffect(() => {
    if (!polling) return;
    const id = window.setInterval(() => {
      load();
      loadDetail();
    }, POLL_MS);
    return () => window.clearInterval(id);
  }, [polling, load, loadDetail]);

  async function cancel(runId: string) {
    setNotice(null);
    try {
      await api.cancelJob(token, runId);
      setNotice({ tone: "ok", text: "Queued refresh cancelled. A new one can start now." });
    } catch (err) {
      setNotice({ tone: "error", text: err instanceof Error ? err.message : String(err) });
    } finally {
      load();
      loadDetail();
    }
  }

  async function refresh() {
    setBusy(true);
    setNotice(null);
    try {
      const accepted = await api.refresh(token);
      setSelected(accepted.run_id);
      setNotice({
        tone: "ok",
        text: "Refresh queued. Progress appears below as GitHub Actions runs it.",
      });
    } catch (err) {
      if (err instanceof ApiError && err.kind === "conflict") {
        const active = typeof err.body?.run_id === "string" ? err.body.run_id : null;
        if (active) setSelected(active);
        setNotice({
          tone: "error",
          text: "A refresh is already running. Its progress is shown below.",
        });
      } else if (err instanceof ApiError && err.kind === "forbidden") {
        setNotice({
          tone: "error",
          text: "Only administrators can refresh data.",
        });
      } else if (err instanceof ApiError && err.status === 502) {
        setNotice({
          tone: "error",
          text: "GitHub did not start the refresh, so nothing ran. Try again.",
        });
      } else {
        setNotice({
          tone: "error",
          text: err instanceof Error ? err.message : String(err),
        });
      }
    } finally {
      setBusy(false);
      load();
    }
  }

  const serving = ops?.serving;
  return (
    <section className="mx-auto max-w-5xl">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">System</h1>
          <p className="mt-1 text-sm text-muted">
            What ChartLens is serving now, and the refresh runs that produce it.
          </p>
        </div>
        {ops?.can_refresh && (
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={busy || Boolean(ops.active_run)}
            className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            {ops.active_run ? "Refresh running…" : busy ? "Starting…" : "Refresh data"}
          </button>
        )}
      </div>

      {notice && (notice.tone === "error" || !shown || isActive(shown.status)) && (
        <p role="status" className={`mt-4 text-sm ${notice.tone === "error" ? "text-down" : ""}`}>
          {notice.text}
        </p>
      )}
      {error && (
        <p role="alert" className="mt-4 text-sm text-down">
          {error}
        </p>
      )}
      {ops && !ops.refresh_configured && ops.last_run === null && (
        <p className="mt-4 text-sm text-muted">
          Refresh from this page is not set up on this server yet; the weekday schedule still runs.
        </p>
      )}

      {ops && (
        <div className="mt-6 grid gap-6 md:grid-cols-2">
          <div className="rounded-lg border border-line bg-surface p-4" data-testid="serving">
            <h2 className="text-sm font-medium text-muted">Serving now</h2>
            {serving ? (
              <>
                <p className="mt-1 text-xl font-semibold">Data through {formatDate(serving.data_as_of)}</p>
                <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
                  <dt className="text-muted">Snapshot</dt>
                  <dd className="font-mono text-xs leading-5 break-all">{serving.meta_version}</dd>
                  <dt className="text-muted">Published</dt>
                  <dd>{formatInstant(serving.snapshot_generated_at)}</dd>
                  <dt className="text-muted">Weekly</dt>
                  <dd className="font-mono text-xs leading-5 break-all">{serving.versions.weekly_version}</dd>
                  <dt className="text-muted">Adjustment</dt>
                  <dd className="font-mono text-xs leading-5 break-all">{serving.versions.adjustment_version}</dd>
                  <dt className="text-muted">Methodology</dt>
                  <dd className="font-mono text-xs leading-5 break-all">{serving.versions.methodology_hash}</dd>
                  <dt className="text-muted">API</dt>
                  <dd>
                    {ops.api_status === "ok" ? "Healthy" : ops.api_status}, version {ops.api_version}
                  </dd>
                </dl>
              </>
            ) : (
              <p className="mt-1 text-sm">
                No snapshot has been published yet. The API is up; data appears after the first successful refresh.
              </p>
            )}
          </div>

          <div className="rounded-lg border border-line bg-surface p-4" data-testid="last-run">
            <h2 className="text-sm font-medium text-muted">Last refresh</h2>
            {ops.last_run ? (
              <>
                <p className="mt-1 text-xl font-semibold">
                  <StatusText status={ops.last_run.status} />
                  <span className="ml-2 text-sm font-normal text-muted">
                    {formatInstant(ops.last_run.completed_at ?? ops.last_run.requested_at)}
                  </span>
                </p>
                <p className="mt-2 text-sm">{outcomeLabel(ops.last_run)}</p>
                {ops.last_run.error_summary && <p className="mt-1 text-sm text-down">{ops.last_run.error_summary}</p>}
                <p className="mt-3 text-sm text-muted">
                  Last successful:{" "}
                  {ops.last_successful_run
                    ? `${formatInstant(ops.last_successful_run.completed_at)}, data through ${formatDate(ops.last_successful_run.data_as_of)}`
                    : "none recorded yet"}
                </p>
              </>
            ) : (
              <p className="mt-1 text-sm">No refresh has been recorded yet.</p>
            )}
          </div>
        </div>
      )}

      {shown && (
        <div className="mt-6 rounded-lg border border-line bg-surface p-4" data-testid="run-detail">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h2 className="font-medium">
              Run <span className="font-mono text-sm">{shown.run_id}</span>
            </h2>
            <p className="text-sm text-muted">
              <StatusText status={shown.status} />
              {isActive(shown.status)
                ? `, ${formatDuration(elapsedSeconds(shown, now))} so far`
                : `, took ${formatDuration(shown.duration_seconds)}`}
            </p>
          </div>
          <p className="mt-1 text-sm text-muted">
            Requested by {requesterLabel(shown)} at {formatInstant(shown.requested_at)}. {outcomeLabel(shown)}.
            {shown.github_run_url && (
              <>
                {" "}
                <a href={shown.github_run_url} className="text-accent underline" target="_blank" rel="noreferrer">
                  Workflow log on GitHub
                </a>
              </>
            )}
          </p>
          {isAdmin && shown.status === "QUEUED" && (
            <p className="mt-2 text-sm text-muted">
              Waiting for GitHub Actions to start it.{" "}
              <button type="button" onClick={() => void cancel(shown.run_id)} className="text-accent underline">
                Cancel this queued refresh
              </button>
            </p>
          )}
          {shown.error_summary && (
            <p className="mt-2 text-sm text-down" data-testid="run-error">
              {shown.error_summary}
            </p>
          )}
          <div className="mt-4">
            <RunStages run={shown} />
          </div>
        </div>
      )}

      {runs && (
        <>
          <h2 className="mt-8 font-medium">Recent runs</h2>
          {runs.length === 0 ? (
            <p className="mt-2 text-sm text-muted">No runs recorded yet.</p>
          ) : (
            <div className="mt-2 overflow-x-auto">
              <table className="w-full text-sm" data-testid="runs">
                <thead className="text-left text-muted">
                  <tr>
                    <th className="py-1 font-normal">Requested</th>
                    <th className="py-1 font-normal">By</th>
                    <th className="py-1 font-normal">Status</th>
                    <th className="py-1 font-normal">Stage</th>
                    <th className="py-1 font-normal">Data through</th>
                    <th className="py-1 text-right font-normal">Took</th>
                    <th className="hidden py-1 pl-4 font-normal lg:table-cell">Finished</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.map((r) => (
                    <tr
                      key={r.run_id}
                      className={`border-t border-line align-top ${r.run_id === focus ? "bg-canvas" : ""}`}
                    >
                      <td className="py-2">
                        <button
                          type="button"
                          onClick={() => setSelected(r.run_id)}
                          className="text-left text-accent underline"
                          aria-label={`Show run ${r.run_id}`}
                        >
                          {formatInstant(r.requested_at)}
                        </button>
                      </td>
                      <td className="py-2">{requesterLabel(r)}</td>
                      <td className="py-2">
                        <StatusText status={r.status} />
                        {r.error_summary && <p className="max-w-xs text-xs text-down">{r.error_summary}</p>}
                      </td>
                      <td className="py-2 text-muted">
                        {isActive(r.status) || r.status === "FAILED" ? stageLabel(r.current_stage) : "—"}
                      </td>
                      <td className="py-2">{formatDate(r.data_as_of)}</td>
                      <td className="num py-2 text-right">{formatDuration(r.duration_seconds)}</td>
                      <td className="hidden py-2 pl-4 text-muted lg:table-cell">{formatInstant(r.completed_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {snapshots && snapshots.length > 0 && (
        <>
          <h2 className="mt-8 font-medium">Snapshot history</h2>
          <p className="mt-1 text-sm text-muted">
            The API serves only the snapshot its pointer names; older ones are kept here as a record.
          </p>
          <table className="mt-2 w-full text-sm" data-testid="snapshots">
            <thead className="text-left text-muted">
              <tr>
                <th className="py-1 font-normal">Snapshot</th>
                <th className="py-1 font-normal">Data through</th>
                <th className="py-1 font-normal">Published</th>
                <th className="py-1 font-normal" />
              </tr>
            </thead>
            <tbody>
              {snapshots.map((s) => (
                <tr key={s.snapshot_id} className="border-t border-line">
                  <td className="py-2 font-mono text-xs">{s.snapshot_id}</td>
                  <td className="py-2">{formatDate(s.data_as_of)}</td>
                  <td className="py-2 text-muted">{formatInstant(s.published_at ?? s.staged_at)}</td>
                  <td className="py-2">{s.live ? "Live" : s.status === "STAGED" ? "Not published" : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}

function StatusText({ status }: { status: RunView["status"] }) {
  const tone =
    status === "FAILED"
      ? "text-down"
      : status === "SUCCEEDED"
        ? "text-up"
        : status === "CANCELLED"
          ? "text-muted"
          : "text-accent";
  return (
    <span className={tone} data-testid="run-status">
      {statusLabel(status)}
    </span>
  );
}
