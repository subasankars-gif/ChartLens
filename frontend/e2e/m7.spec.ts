import { expect, test, type Page } from "@playwright/test";

/**
 * M7 acceptance (ADR-0018) against the real API over the test lake. GitHub Actions is
 * simulated by the test API: a dispatched refresh is claimed and run stage by stage.
 * Authorization is the real API's: a non-admin can see runs but cannot start one.
 */

const API = "http://127.0.0.1:8081/api/v1";
const ADMIN = "e2e-admin:admin@example.com";
const READER = "e2e-reader:reader@example.com";
const KEY = "chartlens-e2e-token";

async function signInAs(page: Page, token: string) {
  await page.goto("/");
  await page.evaluate(({ k, t }) => window.localStorage.setItem(`${k}-next`, t), { k: KEY, t: token });
  await page.getByRole("button", { name: "Sign in with Google" }).click();
}

function call(token: string | null, path: string, method = "GET", body?: unknown) {
  return fetch(`${API}${path}`, {
    method,
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
}

test("an admin refreshes data and follows the run to the end", async ({ page }) => {
  await signInAs(page, ADMIN);
  await page.getByRole("link", { name: "System" }).click();
  await expect(page.getByRole("heading", { name: "System" })).toBeVisible();
  await expect(page.getByTestId("serving")).toContainText("Data through");
  const ops = await (await call(ADMIN, "/system/operations")).json();
  await expect(page.getByTestId("serving")).toContainText(ops.serving.meta_version);
  await expect(page.getByTestId("serving")).toContainText(ops.serving.versions.methodology_hash);

  const started = page.waitForResponse((r) => r.url().endsWith("/refresh/daily"));
  await page.getByRole("button", { name: "Refresh data" }).click();
  const accepted = await (await started).json();
  expect(accepted.status).toBe("QUEUED");
  await expect(page.getByRole("status")).toContainText("Refresh queued");

  // One active run at a time, enforced by the API.
  const second = await call(ADMIN, "/refresh/daily", "POST");
  expect(second.status).toBe(409);
  expect((await second.json()).run_id).toBe(accepted.run_id);

  const detail = page.getByTestId("run-detail");
  await expect(detail).toContainText(accepted.run_id);
  await expect(page.getByRole("button", { name: "Refresh running…" })).toBeDisabled();
  await expect(detail.locator('[data-stage="INGEST"]')).toHaveAttribute("data-status", /RUNNING|SUCCEEDED/, {
    timeout: 15_000,
  });
  await page.screenshot({ path: "test-results/system-running.png", fullPage: true });

  // Polling carries it through to the end without a reload.
  await expect(detail.getByTestId("run-status")).toHaveText("Succeeded", { timeout: 30_000 });
  await expect(detail).toContainText("No new data; the live snapshot was already current");
  for (const stage of ["INGEST", "CORPORATE_ACTIONS", "ADJUSTMENT", "DATA_QUALITY", "WEEKLY", "ANALYSIS", "PUBLISH_SERVING"]) {
    await expect(detail.locator(`[data-stage="${stage}"]`)).toHaveAttribute("data-status", "SUCCEEDED");
  }
  await expect(page.getByTestId("runs")).toContainText("admin@example.com");
  await expect(page.getByTestId("last-run")).toContainText("Succeeded");
  await expect(page.getByRole("button", { name: "Refresh data" })).toBeEnabled();

  const run = await (await call(ADMIN, `/jobs/${accepted.run_id}`)).json();
  expect(run.status).toBe("SUCCEEDED");
  expect(run.stages.map((s: { status: string }) => s.status)).toEqual(Array(7).fill("SUCCEEDED"));
  await page.screenshot({ path: "test-results/system-done.png", fullPage: true });
});

test("an approved user sees runs but cannot refresh, and the API refuses them", async ({ page }) => {
  expect((await call(READER, "/me")).status).toBe(403); // pending after first sign-in
  expect((await call(READER, "/jobs")).status).toBe(403);
  const users = await (await call(ADMIN, "/admin/users")).json();
  const reader = users.find((u: { email: string }) => u.email === "reader@example.com");
  expect((await call(ADMIN, `/admin/users/${reader.uid}`, "PATCH", { enabled: true })).status).toBe(200);

  await signInAs(page, READER);
  await page.getByRole("link", { name: "System" }).click();
  await expect(page.getByTestId("serving")).toContainText("Data through");
  await expect(page.getByRole("button", { name: "Refresh data" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Users" })).toHaveCount(0);
  // The requester's email and the GitHub link are for admins only.
  await expect(page.getByTestId("runs")).toContainText("an administrator");
  await expect(page.getByTestId("runs")).not.toContainText("admin@example.com");

  expect((await call(READER, "/refresh/daily", "POST")).status).toBe(403); // the server decides
  expect((await call(null, "/refresh/daily", "POST")).status).toBe(401);
});
