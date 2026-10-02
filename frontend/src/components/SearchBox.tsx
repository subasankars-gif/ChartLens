"use client";

import { useRouter } from "next/navigation";
import { useEffect, useId, useState } from "react";
import { ApiError, api, type SecuritySummary } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatDate } from "@/lib/format";

export function securityHref(id: string): string {
  return `/security/?id=${encodeURIComponent(id)}`; // the id is opaque: passed through as given
}

/**
 * Search by current or past symbol, ISIN or name (the API matches all of them).
 * `compact` is the header version; the full one adds the instrument scope switch.
 */
export function SearchBox({ compact = false }: { compact?: boolean }) {
  const { token } = useAuth();
  const router = useRouter();
  const listId = useId();
  const [query, setQuery] = useState("");
  const [allInstruments, setAllInstruments] = useState(false);
  const [answer, setAnswer] = useState<{ key: string; results: SecuritySummary[]; error: string | null } | null>(
    null,
  );
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const q = query.trim();
  const key = `${allInstruments ? "all" : "analytical"}|${q}`;
  const current = q && answer?.key === key ? answer : null;
  const results = current?.results ?? [];
  const error = current?.error ?? null;

  useEffect(() => {
    if (!q) return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      api
        .search(token, q, allInstruments ? "all" : "analytical")
        .then((r) => !cancelled && setAnswer({ key, results: r.results, error: null }))
        .catch(
          (err: unknown) =>
            !cancelled &&
            setAnswer({ key, results: [], error: err instanceof ApiError ? err.message : "Search failed." }),
        );
    }, 180);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [q, key, allInstruments, token]);

  function go(s: SecuritySummary) {
    setOpen(false);
    setQuery("");
    router.push(securityHref(s.security_id));
  }

  return (
    <div className="relative">
      <input
        type="search"
        role="combobox"
        aria-expanded={open && results.length > 0}
        aria-controls={listId}
        aria-label="Search by symbol, ISIN or company name"
        placeholder={compact ? "Search symbol, ISIN or name" : "Symbol, ISIN or company name — current or past"}
        value={query}
        onChange={(e) => {
          setQuery(e.target.value);
          setActive(0);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => window.setTimeout(() => setOpen(false), 120)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") setActive((i) => Math.min(i + 1, results.length - 1));
          else if (e.key === "ArrowUp") setActive((i) => Math.max(i - 1, 0));
          else if (e.key === "Enter" && results[active]) go(results[active]);
          else if (e.key === "Escape") setOpen(false);
          else return;
          e.preventDefault();
        }}
        className={`w-full rounded-md border border-line bg-canvas px-3 text-ink placeholder:text-muted ${
          compact ? "py-1.5 text-sm" : "py-3 text-base"
        }`}
      />
      {!compact && (
        <label className="mt-2 flex items-center gap-2 text-sm text-muted">
          <input type="checkbox" checked={allInstruments} onChange={(e) => setAllInstruments(e.target.checked)} />
          Include ETFs, rights entitlements and other instruments (not analysed)
        </label>
      )}
      {open && (results.length > 0 || error) && (
        <ul
          id={listId}
          role="listbox"
          className="absolute z-20 mt-1 max-h-96 w-full overflow-auto rounded-md border border-line bg-surface py-1 shadow-sm"
        >
          {error && <li className="px-3 py-2 text-sm text-down">{error}</li>}
          {results.map((s, i) => (
            <li
              key={s.security_id}
              role="option"
              aria-selected={i === active}
              onMouseDown={(e) => {
                e.preventDefault();
                go(s);
              }}
              onMouseEnter={() => setActive(i)}
              className={`flex cursor-pointer items-baseline gap-3 px-3 py-2 ${i === active ? "bg-canvas" : ""}`}
            >
              <span className="w-28 shrink-0 font-medium">{s.symbol ?? "—"}</span>
              <span className="min-w-0 flex-1 truncate text-sm text-muted">{s.name ?? s.isin ?? ""}</span>
              {s.instrument_type !== "EQUITY_SHARE" && (
                <span className="shrink-0 text-xs text-warn">{s.instrument_type.toLowerCase().replace("_", " ")}</span>
              )}
              {s.listing_status !== "ACTIVE" && (
                <span className="shrink-0 text-xs text-muted">last traded {formatDate(s.last_date)}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
