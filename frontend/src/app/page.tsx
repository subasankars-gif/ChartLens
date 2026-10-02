"use client";

import { useEffect, useState } from "react";
import { SearchBox } from "@/components/SearchBox";
import { api, type ServingStatus } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatDate } from "@/lib/format";

export default function Home() {
  const { token } = useAuth();
  const [status, setStatus] = useState<ServingStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .status(token)
      .then(setStatus)
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, [token]);

  return (
    <div className="mx-auto max-w-3xl pt-10">
      <h1 className="text-2xl font-semibold tracking-tight">Find a security</h1>
      <p className="mt-1 text-sm text-muted">
        {status
          ? `NSE weekly charts, data through ${formatDate(status.data_as_of)}. ${status.counts.analytical ?? 0} equities analysed of ${status.counts.securities ?? 0} securities.`
          : error
            ? `Market data is not available right now: ${error}`
            : "Loading market data status…"}
      </p>
      <div className="mt-6">
        <SearchBox />
      </div>
    </div>
  );
}
