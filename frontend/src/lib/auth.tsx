"use client";

/**
 * Sign-in (who you are). Whether you may see anything is decided by the API, never here
 * (ADR-0017): sign-in only obtains a Firebase ID token for the API to verify.
 *
 * The provider is chosen at build time: `@/lib/auth-provider` resolves to
 * auth-firebase.tsx, or — only in the end-to-end test build — to auth-e2e.tsx
 * (next.config.ts). The test sign-in is therefore absent from the production bundle,
 * which CI checks before every deployment.
 */

import { createContext, useContext } from "react";
import type { TokenSource } from "./api";

export type AuthState =
  | { status: "loading" }
  | { status: "signed_out"; error?: string }
  | { status: "signed_in"; email: string | null };

export type AuthApi = {
  state: AuthState;
  token: TokenSource;
  signIn: () => Promise<void>;
  signOut: () => Promise<void>;
};

export const AuthContext = createContext<AuthApi | null>(null);

export function useAuth(): AuthApi {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth outside <AuthProvider>");
  return value;
}
