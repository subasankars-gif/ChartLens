import { expect, test, type Page } from "@playwright/test";

/**
 * M8 chart layers (ADR-0027) against the real API over the synthetic analysed lake
 * (`E2E_LAKE=synthetic`, scripts/e2e_api.py): layers are drawn from one snapshot, from the
 * engine's stored objects, and nothing is left unplaced.
 */

const API = "http://127.0.0.1:8081/api/v1";
const ADMIN = "e2e-admin:admin@example.com";
const KEY = "chartlens-e2e-token";

async function signInAs(page: Page, token: string) {
  await page.goto("/");
  await page.evaluate(({ k, t }) => window.localStorage.setItem(`${k}-next`, t), { k: KEY, t: token });
  await page.getByRole("button", { name: "Sign in with Google" }).click();
  await expect(page.getByRole("heading", { name: "Find a security" })).toBeVisible();
}

async function apiGet(path: string) {
  return (await fetch(`${API}${path}`, { headers: { Authorization: `Bearer ${ADMIN}` } })).json();
}

const words = (s: string) => s.toLowerCase().replaceAll("_", " ");

test("one /chart request gives bars and the base sections from one snapshot", async ({ page }) => {
  await signInAs(page, ADMIN);
  const first = page.waitForResponse((r) => r.url().includes("/securities/SEC-L/chart?"));
  await page.goto("/security/?id=SEC-L");
  const chart = await (await first).json();
  expect(chart.weekly.meta_version).toBe(chart.meta_version);
  expect(chart.analysis.envelope.meta_version).toBe(chart.meta_version);
  await expect(page.getByTestId("layer-controls")).toBeVisible();
  await expect(page.getByTestId("provenance")).toContainText(chart.analysis.envelope.document_sha256);
});

test("patterns render from stored geometry with their stored status, and nothing is unplaced", async ({ page }) => {
  await signInAs(page, ADMIN);
  await page.goto("/security/?id=SEC-L");
  const sections = page.waitForResponse((r) => r.url().includes("/analysis?sections=patterns"));
  await page.getByTestId("layer-patterns").check();
  const fetched = await (await sections).json();
  await page.getByTestId("pattern-selection").selectOption("all");
  const stored = fetched.document.patterns.patterns;
  await expect(page.getByTestId("panel-patterns").getByTestId("panel-item")).toHaveCount(stored.length);
  await expect(page.getByTestId("unplaced")).toContainText("0 not drawn");

  // A pattern with a measured move: its status is the last stored entry, and its
  // definition fit is shown with its components.
  const index = stored.findIndex((p: { status_history: { measured_move: unknown }[] }) =>
    p.status_history.some((s) => s.measured_move),
  );
  const item = page.getByTestId("panel-patterns").getByTestId("panel-item").nth(index);
  await expect(item.getByTestId("panel-status")).toHaveText(words(stored[index].status_history.at(-1).status));
  await item.getByRole("button").click();
  await expect(item.getByTestId("definition-fit")).toContainText("Definition fit");
  await expect(item).toContainText(stored[index].pattern_id);
  await page.getByTestId("weekly-chart").screenshot({ path: "test-results/m8-patterns.png" });
});

test("every layer on: zero unplaced, in the current and the history view", async ({ page }) => {
  await signInAs(page, ADMIN);
  await page.goto("/security/?id=SEC-L");
  for (const id of [
    "averages",
    "bollinger",
    "oscillator",
    "swings",
    "structure",
    "trend",
    "zones",
    "trendlines",
    "fibonacci",
    "divergence",
    "patterns",
    "breakouts",
    "evidence",
  ]) {
    await page.getByTestId(`layer-${id}`).check();
  }
  await expect(page.getByTestId("panel-breakouts")).toBeVisible();
  await page.waitForLoadState("networkidle"); // every section and event page has arrived
  await expect(page.getByTestId("unplaced")).toContainText("0 not drawn");
  const current = await page.getByTestId("unplaced").textContent();
  await page.getByTestId("weekly-chart").screenshot({ path: "test-results/m8-all-layers.png" });

  const all = page.waitForResponse((r) => r.url().includes("/chart?") && r.url().includes("segments=all"));
  await page.getByTestId("earlier-history").check();
  await all;
  await page.waitForLoadState("networkidle");
  await expect(page.getByTestId("unplaced")).toHaveText(current!);
  await page.getByTestId("weekly-chart").screenshot({ path: "test-results/m8-history.png" });
});

test("the engine's own lists are the defaults", async ({ page }) => {
  await signInAs(page, ADMIN);
  const doc = (await apiGet("/securities/SEC-P/analysis?sections=current")).document;
  await page.goto("/security/?id=SEC-P");
  await page.getByTestId("layer-patterns").check();
  await expect(page.getByTestId("panel-patterns").getByTestId("panel-item")).toHaveCount(doc.current.included_pattern_ids.length);
  await page.getByTestId("layer-trendlines").check();
  await page.getByTestId("layer-fibonacci").check();
  await expect(page.getByTestId("panel-fibonacci").getByTestId("panel-item")).toHaveCount(doc.current.fibonacci_ids.length);
  await expect(page.getByTestId("unplaced")).toContainText("0 not drawn");
  await page.getByTestId("weekly-chart").screenshot({ path: "test-results/m8-included.png" });
});

test("a security outside the analysed universe shows bars and says why", async ({ page }) => {
  await signInAs(page, ADMIN);
  await page.goto("/security/?id=SEC-X");
  await expect(page.getByTestId("analysis-status")).toContainText("not in the analysed universe");
  await expect(page.getByTestId("layer-controls")).toHaveCount(0);
  await expect(page.getByTestId("weekly-chart").locator("canvas").first()).toBeVisible();
});
