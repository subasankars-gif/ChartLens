"use client";

import { useAuth } from "@/lib/auth";

export function SignIn() {
  const { state, signIn } = useAuth();
  const error = state.status === "signed_out" ? state.error : undefined;
  return (
    <section className="mx-auto mt-16 max-w-md rounded-lg border border-line bg-surface p-8">
      <h1 className="text-xl font-semibold tracking-tight">Sign in to ChartLens</h1>
      <p className="mt-2 text-sm leading-6 text-muted">
        ChartLens is private. Sign in with Google; an administrator approves new accounts before any data is shown.
      </p>
      <button
        type="button"
        onClick={() => void signIn()}
        className="mt-6 w-full rounded-md bg-accent px-4 py-2.5 text-sm font-medium text-white hover:opacity-90"
      >
        Sign in with Google
      </button>
      {error && (
        <p role="alert" className="mt-4 text-sm text-down">
          {error}
        </p>
      )}
    </section>
  );
}

export function Pending({ email }: { email: string | null }) {
  const { signOut } = useAuth();
  return (
    <section className="mx-auto mt-16 max-w-md rounded-lg border border-line bg-surface p-8">
      <h1 className="text-xl font-semibold tracking-tight">Waiting for approval</h1>
      <p className="mt-2 text-sm leading-6 text-muted">
        You are signed in{email ? ` as ${email}` : ""}, but this account has not been approved yet. An administrator
        needs to enable it; there is nothing else to do here until then.
      </p>
      <button type="button" onClick={() => void signOut()} className="mt-6 text-sm text-accent underline">
        Sign out
      </button>
    </section>
  );
}

export function AccessError({ message }: { message: string }) {
  return (
    <section role="alert" className="mx-auto mt-16 max-w-md rounded-lg border border-line bg-surface p-8">
      <h1 className="text-xl font-semibold tracking-tight">ChartLens is unavailable</h1>
      <p className="mt-2 text-sm leading-6 text-muted">{message}</p>
      <button type="button" onClick={() => window.location.reload()} className="mt-6 text-sm text-accent underline">
        Try again
      </button>
    </section>
  );
}
