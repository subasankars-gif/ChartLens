import { expect, test, type Page } from "@playwright/test";

/**
 * M6 acceptance (ADR-0017) against the real API over the test lake. Sign-in uses the
 * test build's token; everything else — access decisions included — is the real API.
 */

const API = "http://127.0.0.1:8081/api/v1";
const ADMIN = "e2e-admin:admin@example.com";
const PENDING = "e2e-pending:pending@example.com";
const KEY = "chartlens-e2e-token";

async function signInAs(page: Page, token: string) {
  await page.goto("/");
  await page.evaluate(({ k, t }) => window.localStorage.setItem(`${k}-next`, t), { k: KEY, t: token });
  await page.getByRole("button", { name: "Sign in with Google" }).click();
}

async function apiGet(token: string, path: string) {
  return fetch(`${API}${path}`, { headers: { Authorization: `Bearer ${token}` } });
}

test("signed-out visitors are asked to sign in and see no data", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Sign in to ChartLens" })).toBeVisible();
  await expect(page.getByRole("combobox")).toHaveCount(0);
});

test("a signed-in but unapproved user waits, and the API refuses them too", async ({ page }) => {
  await signInAs(page, PENDING);
  await expect(page.getByRole("heading", { name: "Waiting for approval" })).toBeVisible();
  expect((await apiGet(PENDING, "/system/status")).status).toBe(403); // server-side, not just UI
});

test("search → security page → weekly chart, faithful to the API", async ({ page }) => {
  await signInAs(page, ADMIN);
  await expect(page.getByRole("heading", { name: "Find a security" })).toBeVisible();
  await page.getByRole("combobox").first().fill("INE467B01029"); // DEMERCO by ISIN
  await page.getByRole("option", { name: /DEMERCO/ }).click();

  await expect(page).toHaveURL(/\/security\/\?id=SEC-/);
  const id = new URL(page.url()).searchParams.get("id")!;
  await expect(page.getByTestId("symbol")).toHaveText("DEMERCO");
  await expect(page.getByTestId("usable-from")).toHaveText("24 Jan 2024");
  await expect(page.getByTestId("weekly-chart").locator("canvas").first()).toBeVisible();

  // The legend shows the API's latest bar, exactly.
  const weekly = await (await apiGet(ADMIN, `/securities/${id}/weekly`)).json();
  const last = weekly.bars.at(-1);
  await expect(page.getByTestId("legend-ohlc")).toHaveText(
    `O ${fmt(last.open)} H ${fmt(last.high)} L ${fmt(last.low)} C ${fmt(last.close)}`,
  );

  // Earlier history is opt-in; turning it on asks the API for every segment.
  const all = page.waitForResponse((r) => r.url().includes("/chart?") && r.url().includes("segments=all"));
  await page.getByTestId("earlier-history").check();
  expect((await all).status()).toBe(200);

  await expect(page.getByTestId("provenance")).toContainText(weekly.meta_version);
  await expect(page.getByTestId("provenance")).toContainText(weekly.versions.dq_version);
  await expect(page.getByTestId("findings")).toContainText("unquantified action");
  await page.screenshot({ path: "test-results/security-demerco.png", fullPage: true });
});

test("an admin approves a pending user; approval is enforced by the API", async ({ page, browser }) => {
  // the pending user exists after their first sign-in attempt
  expect((await apiGet(PENDING, "/me")).status).toBe(403);
  expect((await apiGet(PENDING, "/admin/users")).status).toBe(403);
  await signInAs(page, ADMIN);
  await page.getByRole("link", { name: "Users" }).click();
  const row = page.getByRole("row", { name: /pending@example.com/ });
  await expect(row).toContainText("Pending");
  await row.getByRole("button", { name: "Approve" }).click();
  await expect(row).toContainText("Approved");
  expect((await apiGet(PENDING, "/system/status")).status).toBe(200);

  const other = await browser.newPage();
  await signInAs(other, PENDING);
  await expect(other.getByRole("heading", { name: "Find a security" })).toBeVisible();
  await expect(other.getByRole("link", { name: "Users" })).toHaveCount(0);
});

/** The same display rule as src/lib/format.ts, restated independently. */
function fmt(text: string): string {
  const [whole, frac = ""] = text.split(".");
  let f = frac.replace(/0+$/, "");
  if (f.length < 2) f = f.padEnd(2, "0");
  const w = whole!.length <= 3 ? whole! : `${whole!.slice(0, -3).replace(/\B(?=(\d{2})+(?!\d))/g, ",")},${whole!.slice(-3)}`;
  return `${w}.${f}`;
}
