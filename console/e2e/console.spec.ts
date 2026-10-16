import { expect, test, type Page } from "@playwright/test";

const SCREENSHOTS = process.env.SCREENSHOTS === "1";

/** Fails the test on any CSP violation or uncaught page error. */
async function open(page: Page, route: string): Promise<string[]> {
  const problems: string[] = [];
  page.on("pageerror", (error) => problems.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error" && /Content Security Policy|Refused to/i.test(message.text())) {
      problems.push(`csp: ${message.text()}`);
    }
  });
  await page.goto(`/?mock${route}`);
  return problems;
}

async function screenshot(page: Page, name: string): Promise<void> {
  if (SCREENSHOTS) {
    await page.screenshot({ path: `docs/screenshots/${name}.png` });
  }
}

test("assistant plans, asks for approval and publishes", async ({ page }) => {
  const problems = await open(page, "#/assistant");
  await page.getByRole("menuitem", { name: /Fill fast-demo for the next two hours/ }).click();
  await expect(page.getByText("schedule___plan")).toBeVisible();
  await expect(page.getByRole("heading", { name: /Proposed plan/ })).toBeVisible();
  const approve = page.getByRole("button", { name: "Approve", exact: true });
  await expect(approve).toBeVisible();
  await screenshot(page, "assistant");
  await approve.click();
  await expect(page.getByText(/Published \d+ programs to/)).toBeVisible();
  await expect(page.getByRole("heading", { name: /^Schedule/ })).toBeVisible();
  expect(problems).toEqual([]);
});

test("channel plays SSAI and SGAI side by side and fires SGAI beacons", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1800 });
  const problems = await open(page, "#/channels/fast-demo");
  await expect(page.locator("video")).toHaveCount(2);
  const log = page.getByRole("table", { name: "Ad events" });
  // The mock break starts 12 s into both streams (mock/cdn/channel/*.m3u8).
  await expect(log.getByText("Stitched ad started")).toBeVisible({ timeout: 30_000 });
  await expect(log.getByText("INTERSTITIAL_STARTED")).toBeVisible({ timeout: 30_000 });
  await expect(log.getByText("Beacon impression")).toBeVisible();
  await expect(page.getByText(/Ad 1 of 2 · guided/)).toBeVisible();
  await screenshot(page, "channels");
  expect(problems).toEqual([]);
});

test("channel start asks for confirmation and calls the gateway", async ({ page }) => {
  const problems = await open(page, "#/channels/fast-kids");
  await page.getByRole("button", { name: "Start", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("Start channel fast-kids?");
  await dialog.getByRole("button", { name: "Start channel" }).click();
  await expect(page.getByText("fast-kids is running")).toBeVisible();
  expect(problems).toEqual([]);
});

test("library lists assets and ladders", async ({ page }) => {
  const problems = await open(page, "#/library/assets");
  await expect(page.getByRole("table").getByText("Big Buck Bunny")).toBeVisible();
  await screenshot(page, "library");
  await page.getByRole("tab", { name: "Ladders" }).click();
  await expect(page.getByRole("rowheader", { name: "standard@3f9c2a71d0" })).toBeVisible();
  await screenshot(page, "library-ladders");
  expect(problems).toEqual([]);
});
