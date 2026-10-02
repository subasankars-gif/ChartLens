import path from "node:path";
import type { NextConfig } from "next";

// Static export: the site is plain files on Firebase Hosting; all data comes from
// the ChartLens API on Cloud Run. No server-side rendering to host or secure.

// The end-to-end test build replaces Google sign-in with a test token (ADR-0017). It must
// never be built by accident: it needs an explicit second switch.
if (process.env.NEXT_PUBLIC_AUTH_MODE === "e2e" && process.env.CHARTLENS_E2E_BUILD !== "1") {
  throw new Error("NEXT_PUBLIC_AUTH_MODE=e2e is for end-to-end tests only (set CHARTLENS_E2E_BUILD=1)");
}

// Sign-in provider chosen at build time, so the test sign-in never reaches production.
const authProvider =
  process.env.NEXT_PUBLIC_AUTH_MODE === "e2e" ? "./src/lib/auth-e2e.tsx" : "./src/lib/auth-firebase.tsx";

const nextConfig: NextConfig = {
  turbopack: { resolveAlias: { "@/lib/auth-provider": authProvider } },
  webpack: (cfg) => {
    cfg.resolve.alias["@/lib/auth-provider"] = path.resolve(__dirname, authProvider);
    return cfg;
  },
  output: "export",
  trailingSlash: true,
  images: { unoptimized: true },
  reactStrictMode: true,
};

export default nextConfig;
