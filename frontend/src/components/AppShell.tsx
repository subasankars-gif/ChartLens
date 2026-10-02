"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { useAccess } from "@/lib/access";
import { useAuth } from "@/lib/auth";
import { SearchBox } from "./SearchBox";
import { SignIn, Pending, AccessError } from "./Gate";

/**
 * Page frame and the access gate. The gate mirrors what the API says (/me); the API
 * re-authorizes every request regardless (ADR-0017).
 */
export function AppShell({ children }: { children: ReactNode }) {
  const access = useAccess();
  const { signOut } = useAuth();

  return (
    <div className="flex min-h-dvh flex-col">
      <header className="border-b border-line bg-surface">
        <div className="mx-auto flex max-w-7xl items-center gap-4 px-4 py-2.5 sm:px-6">
          <Link href="/" className="shrink-0 text-[15px] font-semibold tracking-tight">
            ChartLens
          </Link>
          {access.status === "ready" ? (
            <>
              <div className="min-w-0 flex-1">
                <SearchBox compact />
              </div>
              <nav className="flex shrink-0 items-center gap-3 text-sm" aria-label="Account">
                {access.user.role === "admin" && (
                  <Link href="/admin/users/" className="text-muted hover:text-ink">
                    Users
                  </Link>
                )}
                <span className="hidden text-muted md:inline">{access.user.email}</span>
                <button type="button" onClick={() => void signOut()} className="text-muted hover:text-ink">
                  Sign out
                </button>
              </nav>
            </>
          ) : (
            <p className="text-sm text-muted">See the structure. Read the trend.</p>
          )}
        </div>
      </header>

      <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 sm:px-6">
        {access.status === "checking" && <p className="text-sm text-muted">Checking your access…</p>}
        {access.status === "signed_out" && <SignIn />}
        {access.status === "pending" && <Pending email={access.email} />}
        {access.status === "error" && <AccessError message={access.message} />}
        {access.status === "ready" && children}
      </main>

      <footer className="border-t border-line">
        <p className="mx-auto max-w-7xl px-4 py-3 text-xs text-muted sm:px-6">
          Charts show exchange data as published, adjusted for corporate actions. ChartLens describes what a
          chart shows; it does not recommend trades. Charting by{" "}
          <a href="https://www.tradingview.com/" className="underline" rel="noreferrer" target="_blank">
            TradingView
          </a>
          .
        </p>
      </footer>
    </div>
  );
}
