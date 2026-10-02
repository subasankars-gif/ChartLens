"use client";

import type { ReactNode } from "react";
import { AccessProvider } from "@/lib/access";
import { AuthProvider } from "@/lib/auth-provider";
import { AppShell } from "./AppShell";

export function Providers({ children }: { children: ReactNode }) {
  return (
    <AuthProvider>
      <AccessProvider>
        <AppShell>{children}</AppShell>
      </AccessProvider>
    </AuthProvider>
  );
}
