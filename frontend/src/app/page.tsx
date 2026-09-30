import { SystemStatus } from "@/components/SystemStatus";

export default function Home() {
  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-8 px-4 py-6 sm:px-6">
      <header className="flex items-baseline justify-between border-b border-line pb-4">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">ChartLens</h1>
          <p className="text-sm text-muted">See the structure. Read the trend.</p>
        </div>
        <span className="font-mono text-xs text-muted">Phase 1 · Foundation</span>
      </header>

      <div className="flex flex-col gap-2">
        <label htmlFor="security-search" className="text-xs font-medium uppercase tracking-wider text-muted">
          Security
        </label>
        <input
          id="security-search"
          type="search"
          disabled
          placeholder="Search NSE equities — available once the security master is loaded"
          className="w-full rounded-md border border-line bg-surface px-3 py-2 text-sm placeholder:text-muted disabled:cursor-not-allowed"
        />
      </div>

      <SystemStatus />
    </div>
  );
}
