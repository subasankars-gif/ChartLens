import { defineConfig } from "@playwright/test";

/**
 * End-to-end tests (ADR-0017): the built static site (test sign-in build, `pnpm e2e:build`)
 * against the real API over a small test lake (scripts/e2e_api.py).
 */
export default defineConfig({
  testDir: "e2e",
  timeout: 90_000,
  testIgnore: process.env.E2E_LAKE ? ["m6.spec.ts"] : ["real-data.spec.ts"],
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: "http://localhost:3000",
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_PATH
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH }
      : undefined,
    screenshot: "only-on-failure",
  },
  webServer: [
    {
      command: `uv run --no-sync python ../scripts/e2e_api.py 8081 ${process.env.E2E_LAKE ?? ""}`,
      url: "http://127.0.0.1:8081/api/v1/health",
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: "python3 -m http.server 3000 --bind 127.0.0.1 -d out",
      url: "http://127.0.0.1:3000/",
      reuseExistingServer: false,
    },
  ],
});
