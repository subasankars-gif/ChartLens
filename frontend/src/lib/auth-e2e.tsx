"use client";

/**
 * Test sign-in for the end-to-end build only (ADR-0017): the token is read from
 * localStorage. Never part of a production bundle (see auth.tsx).
 */

import { useCallback, useMemo, useSyncExternalStore, type ReactNode } from "react";
import type { TokenSource } from "./api";
import { AuthContext, type AuthState } from "./auth";

export const E2E_TOKEN_KEY = "chartlens-e2e-token";

const E2E_EVENT = "chartlens-e2e-auth";

function subscribeE2e(onChange: () => void): () => void {
  window.addEventListener("storage", onChange);
  window.addEventListener(E2E_EVENT, onChange);
  return () => {
    window.removeEventListener("storage", onChange);
    window.removeEventListener(E2E_EVENT, onChange);
  };
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const stored = useSyncExternalStore(
    subscribeE2e,
    () => window.localStorage.getItem(E2E_TOKEN_KEY) ?? "",
    () => null,
  );
  const state: AuthState = useMemo(
    () =>
      stored === null
        ? { status: "loading" }
        : stored
          ? { status: "signed_in", email: stored.split(":")[1] ?? null }
          : { status: "signed_out" },
    [stored],
  );

  const token = useCallback<TokenSource>(async () => window.localStorage.getItem(E2E_TOKEN_KEY), []);
  const signIn = useCallback(async () => {
    const t = window.localStorage.getItem(`${E2E_TOKEN_KEY}-next`) ?? "e2e-admin:admin@example.com";
    window.localStorage.setItem(E2E_TOKEN_KEY, t);
    window.dispatchEvent(new Event(E2E_EVENT));
  }, []);
  const signOut = useCallback(async () => {
    window.localStorage.removeItem(E2E_TOKEN_KEY);
    window.dispatchEvent(new Event(E2E_EVENT));
  }, []);

  const value = useMemo(() => ({ state, token, signIn, signOut }), [state, token, signIn, signOut]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
