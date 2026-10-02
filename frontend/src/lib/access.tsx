"use client";

/**
 * Access as the API reports it (ADR-0016/17): signed in is not enough; the API's /me
 * answers 403 "access pending approval" until an admin enables the user. The UI only
 * mirrors that answer — every data request is authorized by the API again.
 */

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { ApiError, api, type User } from "./api";
import { useAuth } from "./auth";

export type Access =
  | { status: "checking" }
  | { status: "signed_out" }
  | { status: "pending"; email: string | null }
  | { status: "ready"; user: User }
  | { status: "error"; message: string };

const AccessContext = createContext<Access>({ status: "checking" });

export function useAccess(): Access {
  return useContext(AccessContext);
}

export function AccessProvider({ children }: { children: ReactNode }) {
  const { state, token } = useAuth();
  // The answer for the sign-in it was asked for; a newer sign-in state shows "checking".
  const [answer, setAnswer] = useState<{ for: unknown; access: Access } | null>(null);

  useEffect(() => {
    if (state.status !== "signed_in") return;
    let cancelled = false;
    api
      .me(token)
      .then((user) => !cancelled && setAnswer({ for: state, access: { status: "ready", user } }))
      .catch((err: unknown) => {
        if (cancelled) return;
        const access: Access =
          err instanceof ApiError && err.kind === "pending"
            ? { status: "pending", email: state.email }
            : err instanceof ApiError && err.kind === "signed_out"
              ? { status: "signed_out" }
              : { status: "error", message: err instanceof Error ? err.message : String(err) };
        setAnswer({ for: state, access });
      });
    return () => {
      cancelled = true;
    };
  }, [state, token]);

  const access: Access =
    state.status === "loading"
      ? { status: "checking" }
      : state.status === "signed_out"
        ? { status: "signed_out" }
        : answer?.for === state
          ? answer.access
          : { status: "checking" };
  return <AccessContext.Provider value={access}>{children}</AccessContext.Provider>;
}
