"use client";

/**
 * "What the chart shows" (ADR-0028 §7): the published explanation's claims, in the order
 * the engine stored them, with their text exactly as stored. The browser never rewords,
 * reorders or completes a claim. Each claim can show the stored facts it quotes (where in
 * the analysis document each value lives), and pointing at a claim focuses the object it
 * is about on the chart, turning its layer on.
 */

import { useState } from "react";
import type { ExplanationResponse } from "@/lib/api";
import type { LayerId } from "@/lib/layers/types";

type Quoted = { name: string; ref: string; field: string; kind: string; value: unknown };
type Reference = { id: string; pointer: string };
export type StoredClaim = {
  claim_id: string;
  claim_type: string;
  template_id: string;
  subject: string;
  references: Reference[];
  quoted_values: Quoted[];
  rendered_text: string;
  known_at: string | null;
  provisional: boolean;
};

/** Which chart layer draws the object a claim is about (presentation only). */
const LAYER_OF: Record<string, LayerId | undefined> = {
  TREND_STATE: "trend",
  ZONE: "zones",
  ZONE_ROLE_REVERSED: "zones",
  ACTIVE_TRENDLINE: "trendlines",
  FIBONACCI: "fibonacci",
  FIBONACCI_LEVEL: "fibonacci",
  PATTERN: "patterns",
  PATTERN_CONFIRMATION: "patterns",
  PATTERN_INVALIDATION: "patterns",
  PATTERN_MEASURED_MOVE: "patterns",
  PATTERN_FIT: "patterns",
  PATTERN_TAG: "patterns",
};

/** The id of the chart object a claim is about: its subject, or for a relevance tag the
 * pattern it references. */
export function focusOf(claim: StoredClaim): string | null {
  const subject = claim.references.find((r) => r.pointer === claim.subject);
  if (claim.claim_type === "PATTERN_TAG") {
    return claim.references.find((r) => r.pointer.startsWith("/patterns/"))?.id ?? null;
  }
  return subject && !subject.id.startsWith("/") ? subject.id : null;
}

export function ExplanationPanel({
  explanation,
  focused,
  onFocus,
}: {
  explanation: ExplanationResponse;
  focused: string | null;
  onFocus: (id: string | null, layer: LayerId | null) => void;
}) {
  const claims = (explanation.explanation as { claims: StoredClaim[] }).claims;
  const [open, setOpen] = useState<string | null>(null);
  return (
    <section className="flex flex-col gap-2 rounded-lg border border-line bg-surface p-4" aria-label="What the chart shows">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold">What the chart shows</h2>
        <p className="text-xs text-muted">
          Each sentence restates stored facts of this analysis; nothing is calculated or judged here.
        </p>
      </div>
      <ol className="flex flex-col divide-y divide-line text-sm" data-testid="claims">
        {claims.map((c) => {
          const target = focusOf(c);
          const isFocused = target !== null && target === focused;
          return (
            <li key={c.claim_id} className={`py-1.5 ${isFocused ? "bg-canvas" : ""}`} data-testid="claim">
              <div className="flex flex-wrap items-baseline gap-x-3">
                {target ? (
                  <button
                    type="button"
                    className="text-left hover:underline"
                    onClick={() => onFocus(isFocused ? null : target, LAYER_OF[c.claim_type] ?? null)}
                    aria-pressed={isFocused}
                    data-testid="claim-text"
                  >
                    {c.rendered_text}
                  </button>
                ) : (
                  <span data-testid="claim-text">{c.rendered_text}</span>
                )}
                {c.provisional && <span className="text-xs text-warn">provisional</span>}
                <button
                  type="button"
                  className="text-xs text-muted hover:text-ink"
                  onClick={() => setOpen(open === c.claim_id ? null : c.claim_id)}
                  aria-expanded={open === c.claim_id}
                  data-testid="claim-facts-toggle"
                >
                  {open === c.claim_id ? "Hide facts" : "Facts"}
                </button>
              </div>
              {open === c.claim_id && (
                <table className="mt-1 w-full text-xs" data-testid="claim-facts">
                  <tbody>
                    {c.quoted_values.map((q) => (
                      <tr key={q.name} className="border-t border-line">
                        <td className="py-0.5 pr-3 text-muted">{q.name.replaceAll("_", " ")}</td>
                        <td className="num pr-3 font-mono">{JSON.stringify(q.value)}</td>
                        <td className="break-all font-mono text-muted">
                          {q.ref}
                          {q.field}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </li>
          );
        })}
      </ol>
      <p className="text-xs text-muted">
        Explanation <span className="font-mono">{explanation.envelope.explain_version}</span>, bound to analysis document{" "}
        <span className="font-mono break-all">{explanation.envelope.document_sha256.slice(0, 12)}</span>.
      </p>
    </section>
  );
}
