"use client";

import { useEffect, useState } from "react";
import { apiBaseUrl, fetchHealth, type Health } from "@/lib/api";

type State =
  | { kind: "loading" }
  | { kind: "ok"; health: Health }
  | { kind: "error"; message: string };

export function SystemStatus() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    fetchHealth()
      .then((health) => !cancelled && setState({ kind: "ok", health }))
      .catch((err: unknown) => {
        if (!cancelled) setState({ kind: "error", message: err instanceof Error ? err.message : String(err) });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const dot =
    state.kind === "ok" ? "bg-up" : state.kind === "error" ? "bg-down" : "bg-muted animate-pulse";
  const label = state.kind === "ok" ? "API online" : state.kind === "error" ? "API offline" : "Checking API";

  return (
    <section aria-labelledby="system-status" className="rounded-md border border-line bg-surface">
      <header className="flex items-center justify-between border-b border-line px-4 py-2.5">
        <h2 id="system-status" className="text-xs font-medium uppercase tracking-wider text-muted">
          System status
        </h2>
        <span className="flex items-center gap-2 text-xs">
          <span className={`size-2 rounded-full ${dot}`} aria-hidden />
          {label}
        </span>
      </header>

      {state.kind === "error" && (
        <p className="px-4 py-3 text-sm text-muted">
          {state.message}. Start it with <code className="font-mono text-ink">uv run poe api</code>.
        </p>
      )}

      {state.kind === "ok" && (
        <dl className="grid grid-cols-2 gap-x-6 gap-y-2 px-4 py-3 text-sm sm:grid-cols-4">
          <Field term="Exchange" value={state.health.exchange} />
          <Field term="Environment" value={state.health.environment} />
          <Field term="Methodology" value={state.health.methodology_hash} mono />
          <Field term="Engine" value={`v${state.health.versions.engine}`} mono />
        </dl>
      )}

      <p className="border-t border-line px-4 py-2 font-mono text-[11px] text-muted">{apiBaseUrl()}</p>
    </section>
  );
}

function Field({ term, value, mono = false }: { term: string; value: string; mono?: boolean }) {
  return (
    <div>
      <dt className="text-xs text-muted">{term}</dt>
      <dd className={mono ? "font-mono" : undefined}>{value}</dd>
    </div>
  );
}
