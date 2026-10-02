"use client";

/**
 * Admin → Users. A convenience over the admin API: the API authorizes every call (only
 * admins may list or change users), whatever this page shows (ADR-0017).
 */

import { useCallback, useEffect, useState } from "react";
import { ApiError, api, type User } from "@/lib/api";
import { useAccess } from "@/lib/access";
import { useAuth } from "@/lib/auth";
import { formatDate } from "@/lib/format";

export default function UsersPage() {
  const { token } = useAuth();
  const access = useAccess();
  const [users, setUsers] = useState<User[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .users(token)
      .then(setUsers)
      .catch((err: unknown) =>
        setError(err instanceof ApiError && err.kind === "forbidden" ? "Only administrators can manage users." : String(err)),
      );
  }, [token]);
  useEffect(load, [load]);

  async function change(uid: string, patch: { enabled?: boolean; role?: "admin" | "user" }) {
    try {
      await api.updateUser(token, uid, patch);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const self = access.status === "ready" ? access.user.uid : null;
  return (
    <section className="mx-auto max-w-4xl">
      <h1 className="text-2xl font-semibold tracking-tight">Users</h1>
      <p className="mt-1 text-sm text-muted">
        Anyone who signs in with Google appears here as pending. Approve them to give access to ChartLens data.
      </p>
      {error && (
        <p role="alert" className="mt-4 text-sm text-down">
          {error}
        </p>
      )}
      {users && (
        <table className="mt-6 w-full text-sm" data-testid="users">
          <thead className="text-left text-muted">
            <tr>
              <th className="py-1 font-normal">Email</th>
              <th className="py-1 font-normal">Since</th>
              <th className="py-1 font-normal">Access</th>
              <th className="py-1 font-normal">Role</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.uid} className="border-t border-line">
                <td className="py-2">{u.email ?? u.uid}</td>
                <td className="py-2 text-muted">{formatDate(u.created_at)}</td>
                <td className={`py-2 ${u.enabled ? "" : "text-warn"}`}>{u.enabled ? "Approved" : "Pending"}</td>
                <td className="py-2">{u.role === "admin" ? "Administrator" : "User"}</td>
                <td className="py-2 text-right">
                  {u.uid !== self && (
                    <button
                      type="button"
                      onClick={() => void change(u.uid, { enabled: !u.enabled })}
                      className="text-accent underline"
                    >
                      {u.enabled ? "Revoke access" : "Approve"}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
