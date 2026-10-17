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
  // The published block is folded into the schedule read before planning, so the panel still shows all of it.
  const schedule = page.getByRole("heading", { name: /^Schedule/ });
  await expect(schedule).toBeVisible();
  await expect(page.getByText("Published schedule of fast-demo from now on")).toBeVisible();
  await expect(page.getByRole("img", { name: "Succeeded" })).toHaveCount(4);
  expect(problems).toEqual([]);
});

test("a rejected publish is shown as declined, not as succeeded", async ({ page }) => {
  const problems = await open(page, "#/assistant");
  await page.getByRole("menuitem", { name: /Fill fast-demo for the next two hours/ }).click();
  await page.getByRole("button", { name: "Reject", exact: true }).click();
  await expect(page.getByText(/I left that alone/)).toBeVisible();
  await expect(page.getByRole("img", { name: "Declined" })).toHaveCount(1);
  await expect(page.getByRole("img", { name: "Succeeded" })).toHaveCount(3); // get_schedule, search, plan
  expect(problems).toEqual([]);
});

test("agent tables render as tables", async ({ page }) => {
  const problems = await open(page, "#/assistant");
  await page.getByRole("menuitem", { name: /Which channels are on air/ }).click();
  const table = page.getByRole("region", { name: "Conversation" }).getByRole("table");
  await expect(table.getByRole("columnheader", { name: "Ladder" })).toBeVisible();
  await expect(table.getByRole("cell", { name: "fast-kids" })).toBeVisible();
  await expect(page.getByText("|---|")).toHaveCount(0);
  expect(problems).toEqual([]);
});

test("two approvals are answered one run at a time, each with one card", async ({ page }) => {
  const problems = await open(page, "#/assistant");
  const prompt = page.getByRole("textbox", { name: "Message to the scheduling agent" });
  await prompt.fill("Stop fast-demo and fast-kids");
  await prompt.press("Enter");
  const approve = page.getByRole("button", { name: "Approve", exact: true });
  await expect(approve).toHaveCount(2);

  await approve.first().click();
  // The other card waits while the first decision runs, then the agent asks for it again under the same id.
  await expect(approve).toHaveCount(1);
  await expect(approve).toBeDisabled();
  await expect(approve).toBeEnabled();
  await expect(page.getByRole("button", { name: "Reject", exact: true })).toHaveCount(1);
  await expect(page.getByText("Approval required")).toHaveCount(2);

  await approve.click();
  await expect(page.getByText("fast-kids is stopped.")).toBeVisible();
  await expect(page.getByRole("img", { name: "Succeeded" })).toHaveCount(2);
  expect(problems).toEqual([]);
});

test("channel plays its SSAI stream through the stitched ad break", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1600 });
  const problems = await open(page, "#/channels/fast-demo");
  await expect(page.locator("video")).toHaveCount(1);
  // For the break's EXT-X-DATERANGE, hls.js adds a hidden metadata track with a data: source: media-src allows it.
  await expect(page.locator('video > track[kind="metadata"]')).toHaveAttribute("src", "data:,WEBVTT");
  const log = page.getByRole("table", { name: "Ad events" });
  // The mock break starts 12 s into the stream and lasts 8 s (mock/cdn/channel/hls.m3u8).
  await expect(log.getByText("Stitched ad started")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("Ad · stitched")).toBeVisible();
  await screenshot(page, "channels");
  // Both ads play through, their muxed AAC behind discontinuities, and the content resumes.
  await expect(log.getByText("Stitched ad ended")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("Content", { exact: true })).toBeVisible();
  expect(problems).toEqual([]);
});

test("a stopped channel is off air until it starts, then plays without a reload", async ({ page }) => {
  const problems = await open(page, "#/channels/fast-kids");
  await expect(page.getByText("Off air", { exact: true })).toHaveCount(1);
  await expect(page.getByText("fast-kids is stopped", { exact: true })).toHaveCount(1);
  await expect(page.locator("video")).toHaveCount(0);
  const guide = page.getByRole("table", { name: "Programs" });
  await expect(guide.getByText("Published", { exact: true }).first()).toBeVisible();
  await expect(guide.getByText("On air", { exact: true })).toHaveCount(0); // nothing airs on a stopped channel

  await page.getByRole("button", { name: "Start", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("Start channel fast-kids?");
  await dialog.getByRole("button", { name: "Start channel" }).click();
  await expect(page.getByText("fast-kids is running")).toBeVisible();
  // The start skipped the program it would have joined midway, and the guide shows it at once.
  await expect(page.getByText(/Skipped because their start had passed: .+ \(.+\)\. Filler plays/)).toBeVisible();
  await expect(guide.getByText("Skipped", { exact: true })).toHaveCount(1);
  await expect(page.locator("video")).toHaveCount(1);
  const rows = page.getByRole("table", { name: "Ad events" }).getByRole("row");
  await expect(rows.filter({ hasText: "MANIFEST_PARSED" })).toHaveCount(1);

  await page.getByRole("button", { name: "Stop", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Stop channel" }).click();
  await expect(page.getByText("Off air", { exact: true })).toHaveCount(1);
  await expect(page.locator("video")).toHaveCount(0);
  expect(problems).toEqual([]);
});

test("a player reloads a stream that fails while the channel starts, with backoff", async ({ page }) => {
  // What a starting channel answers through the CDN; hls.js retries once itself, then gives up (fatal).
  let requests = 0;
  await page.route("**/mock/cdn/channel/hls.m3u8", (route) =>
    ++requests <= 2
      ? route.fulfill({ status: 504, contentType: "application/json", body: '{"message":"failed to generate manifest: Unable to obtain template playlist."}' })
      : route.continue(),
  );
  const problems = await open(page, "#/channels/fast-demo");
  const rows = page.getByRole("table", { name: "Ad events" }).getByRole("row");
  await expect(rows.filter({ hasText: "Retry 1" })).toContainText("manifestLoadError (HTTP 504): reloading in 2 s");
  await expect(rows.filter({ hasText: "MANIFEST_PARSED" })).toHaveCount(1, { timeout: 10_000 });
  await expect(page.locator("video")).toHaveCount(1);
  expect(requests).toBeGreaterThanOrEqual(3);
  expect(problems).toEqual([]);
});

test("the program guide marks programs skipped by a late start", async ({ page }) => {
  const problems = await open(page, "#/channels/fast-demo");
  // fast-demo started 20 minutes ago (src/mock/fixtures.ts): the program planned before then never aired.
  const guide = page.getByRole("table", { name: "Programs" });
  await expect(guide.getByText("Skipped", { exact: true }).first()).toBeVisible();
  await expect(guide.getByText("On air", { exact: true })).toHaveCount(1);
  await expect(page.getByRole("list", { name: "Program timeline" }).getByText(/· skipped$/).first()).toBeAttached();
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
