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

test("the explanation panel shows the stored claims, in order, and their facts (ADR-0028)", async ({ page }) => {
  await signInAs(page, ADMIN);
  const stored = await apiGet("/securities/SEC-P/explanations");
  const claims = stored.explanation.claims as { rendered_text: string; claim_type: string; quoted_values: unknown[] }[];
  expect(claims.length).toBeGreaterThan(3);
  await page.goto("/security/?id=SEC-P");
  const shown = page.getByTestId("claims").getByTestId("claim-text");
  await expect(shown).toHaveCount(claims.length);
  await expect(shown).toHaveText(claims.map((c) => c.rendered_text)); // verbatim, stored order

  // A pattern claim focuses its pattern and turns the patterns layer on.
  const index = claims.findIndex((c) => c.claim_type === "PATTERN");
  expect(index).toBeGreaterThanOrEqual(0);
  await shown.nth(index).click();
  await expect(page.getByTestId("layer-patterns")).toBeChecked();
  await page.waitForLoadState("networkidle");
  await expect(page.getByTestId("unplaced")).toContainText("0 not drawn");

  // Its facts are the quoted stored values.
  const claim = page.getByTestId("claims").getByTestId("claim").nth(index);
  await claim.getByTestId("claim-facts-toggle").click();
  await expect(claim.getByTestId("claim-facts").locator("tr")).toHaveCount(claims[index]!.quoted_values.length);
  await page.screenshot({ path: "test-results/m8-explanations.png", fullPage: true });
});

test("the current weekly state restates the snapshot's own current analysis, above the chart (Issue 2)", async ({ page }) => {
  await signInAs(page, ADMIN);
  const first = page.waitForResponse((r) => r.url().includes("/securities/SEC-P/chart?"));
  await page.goto("/security/?id=SEC-P");
  const chart = await (await first).json();
  const doc = (await apiGet("/securities/SEC-P/analysis?sections=current,structure")).document;
  const panel = page.getByTestId("current-state");
  await expect(panel).toBeVisible();

  // One snapshot: the panel names the chart's own snapshot and the engine's state date
  // (the last complete weekly bar), never the forming week.
  await expect(panel.getByTestId("state-snapshot")).toHaveText(chart.meta_version);
  const stateDate = doc.current.state_date as string;
  const [y, m, d] = stateDate.split("-").map(Number);
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  await expect(panel.getByTestId("state-date")).toHaveText(`${d} ${months[m! - 1]} ${y}`);
  const last = chart.weekly.bars.at(-1);
  if (!last.is_complete) {
    expect(last.last_session_date > stateDate).toBe(true); // the forming week is after the state date
    await expect(panel.getByTestId("state-forming")).toBeVisible();
  }

  // The synthetic lake's data is old: its real date is shown as stale, not as today.
  await expect(panel.getByTestId("state-stale")).toContainText("days old");

  // Claims grouped under headings, verbatim and in stored order (the panel holds them).
  await expect(panel.getByTestId("claim-group").first()).toBeVisible();
  const stored = (await apiGet("/securities/SEC-P/explanations")).explanation.claims as { rendered_text: string }[];
  await expect(panel.getByTestId("claims").getByTestId("claim-text")).toHaveText(stored.map((c) => c.rendered_text));

  // Changes since the engine's trend `since`: exactly the stored structure events, and
  // each breakout dataset on its own.
  const since = doc.current.trend_since as string | null;
  const events = (doc.structure.events as { bar_date: string }[]).filter((e) => since !== null && e.bar_date >= since);
  await expect(panel.getByTestId("state-structure-events")).toContainText(`(${events.length})`);
  if (since) {
    const pattern = await apiGet(`/securities/SEC-P/breakout-events?source=pattern&from=${since}&limit=500`);
    await expect(panel.getByTestId("state-pattern-breakouts")).toContainText(`(${pattern.rows.length})`);
    await expect(panel.getByTestId("state-level-breakouts")).toBeVisible();
  }
  await expect(panel.getByTestId("state-limits")).toContainText("would change");

  // It sits above the chart.
  const panelBox = await panel.boundingBox();
  const chartBox = await page.getByTestId("weekly-chart").boundingBox();
  expect(panelBox!.y).toBeLessThan(chartBox!.y);
  await page.screenshot({ path: "test-results/current-state.png", fullPage: true });
});

test("history: every returned bar is on the chart, a short analysed history is explained, and earlier bars stay reachable (Issue 1)", async ({ page }) => {
  await signInAs(page, ADMIN);
  const valid = await apiGet("/securities/SEC-L/weekly?segments=valid");
  const all = await apiGet("/securities/SEC-L/weekly?segments=all");
  const detail = await apiGet("/securities/SEC-L");
  expect(all.bars.length).toBeGreaterThan(valid.bars.length);
  await page.goto("/security/?id=SEC-L");

  // The default chart is the current segment: all of its bars, none cut by the view.
  const range = page.getByTestId("chart-range");
  await expect(range).toContainText(`${valid.bars.length} weekly bars on the chart`);
  const current = detail.segments.find((s: { continuity_segment_id: string }) => s.continuity_segment_id === detail.current_segment_id);
  await expect(page.getByTestId("history-notice")).toContainText("no analysis crosses it");
  await expect(page.getByTestId("history-notice")).toContainText(current.segment_start.slice(0, 4));

  // "All" and the earlier segments: older bars are on the chart, not removed.
  await page.getByTestId("range-All").click();
  await expect(page.getByTestId("range-All")).toHaveAttribute("aria-pressed", "true");
  await page.getByTestId("earlier-history").check();
  await expect(range).toContainText(`${all.bars.length} weekly bars on the chart`);

  // Support and resistance in the published analysis are all drawn: none lost on the way.
  const levels = (await apiGet("/securities/SEC-L/analysis?sections=levels")).document.levels;
  await page.getByTestId("layer-zones").check();
  await expect(page.getByTestId("panel-zones").getByTestId("panel-item")).toHaveCount(levels.zones.length);
  await expect(page.getByTestId("unplaced")).toContainText("0 not drawn");
});

test("a security with a single segment shows exactly its own history and no break notice", async ({ page }) => {
  await signInAs(page, ADMIN);
  const weekly = await apiGet("/securities/SEC-X/weekly?segments=valid");
  await page.goto("/security/?id=SEC-X");
  await expect(page.getByTestId("chart-range")).toContainText(`${weekly.bars.length} weekly bars on the chart`);
  await expect(page.getByTestId("history-notice")).toHaveCount(0);
  await expect(page.getByTestId("current-state")).toHaveCount(0); // not analysed: no state is invented
});
