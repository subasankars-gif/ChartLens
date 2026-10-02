"use client";

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import type { TokenSource } from "./api";
import { AuthContext, type AuthState } from "./auth";
import { config } from "./config";

/** Google sign-in through Firebase Authentication (popup). */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });
  const [auth, setAuth] = useState<import("firebase/auth").Auth | null>(null);

  useEffect(() => {
    let unsubscribe: (() => void) | undefined;
    let cancelled = false;
    (async () => {
      if (!config.firebase.apiKey || !config.firebase.projectId) {
        setState({ status: "signed_out", error: "Sign-in is not configured for this build." });
        return;
      }
      const [{ initializeApp, getApps }, authModule] = await Promise.all([
        import("firebase/app"),
        import("firebase/auth"),
      ]);
      const app = getApps()[0] ?? initializeApp(config.firebase);
      const a = authModule.getAuth(app);
      if (cancelled) return;
      setAuth(a);
      unsubscribe = authModule.onAuthStateChanged(a, (user) =>
        setState(user ? { status: "signed_in", email: user.email } : { status: "signed_out" }),
      );
    })().catch((err: unknown) =>
      setState({ status: "signed_out", error: err instanceof Error ? err.message : String(err) }),
    );
    return () => {
      cancelled = true;
      unsubscribe?.();
    };
  }, []);

  const token = useCallback<TokenSource>(async () => (auth?.currentUser ? auth.currentUser.getIdToken() : null), [auth]);

  const signIn = useCallback(async () => {
    if (!auth) return;
    const { GoogleAuthProvider, signInWithPopup } = await import("firebase/auth");
    try {
      await signInWithPopup(auth, new GoogleAuthProvider());
    } catch (err) {
      const code = (err as { code?: string }).code ?? "";
      if (code !== "auth/popup-closed-by-user" && code !== "auth/cancelled-popup-request") {
        setState({ status: "signed_out", error: "Google sign-in did not complete. Try again." });
      }
    }
  }, [auth]);

  const signOut = useCallback(async () => {
    if (auth) await (await import("firebase/auth")).signOut(auth);
  }, [auth]);

  const value = useMemo(() => ({ state, token, signIn, signOut }), [state, token, signIn, signOut]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

