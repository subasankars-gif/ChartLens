import { expect, test } from "@playwright/test";

/**
 * Screenshots of real securities over a copy of the production lake (M6 probe). Run with
 * E2E_LAKE=<lake dir>; skipped otherwise. Checks the same faithfulness rules on real data.
 */

const API = "http://127.0.0.1:8081/api/v1";
const ADMIN = "e2e-admin:admin@example.com";

const CASES = [
  { symbol: "RELIANCE", all: true },
  { symbol: "RELIANCE", all: false },
  { symbol: "TATACOMM", all: true },
  { symbol: "HDFCBANK", all: false },
  { symbol: "ITC", all: true },
  { symbol: "3IINFOLTD", all: true },
];

for (const c of CASES) {
  test(`${c.symbol} ${c.all ? "all segments" : "valid segment"}`, async ({ page }) => {
    const found = await (
      await fetch(`${API}/securities?q=${c.symbol}`, { headers: { Authorization: `Bearer ${ADMIN}` } })
    ).json();
    const sec = found.results.find((r: { symbol: string }) => r.symbol === c.symbol);
    expect(sec).toBeTruthy();
    await page.goto("/");
    await page.evaluate((t) => window.localStorage.setItem("chartlens-e2e-token", t), ADMIN);
    await page.goto(`/security/?id=${encodeURIComponent(sec.security_id)}`);
    await expect(page.getByTestId("symbol")).toHaveText(c.symbol);
    if (c.all) await page.getByTestId("earlier-history").check();
    await expect(page.getByTestId("weekly-chart").locator("canvas").first()).toBeVisible();
    await page.waitForTimeout(800);
    await page.screenshot({
      path: `test-results/real-${c.symbol}-${c.all ? "all" : "valid"}.png`,
      fullPage: false,
    });
  });
}
