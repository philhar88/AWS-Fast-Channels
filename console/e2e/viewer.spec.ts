import { expect, test, type Page } from "@playwright/test";

const SCREENSHOTS = process.env.SCREENSHOTS === "1";

/** Opens the viewer in mock mode; fails the test on any CSP violation or uncaught page error. */
async function open(page: Page, route = "#/personas"): Promise<string[]> {
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

async function signIn(page: Page, name: string): Promise<void> {
  await page.getByRole("button", { name: `Watch as ${name}` }).click();
  await page.getByRole("textbox", { name: "Password" }).fill("demo-password");
  await page.getByRole("dialog").getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: `Recommended channels for ${name}` })).toBeVisible();
}

const railChannels = (page: Page) =>
  page.getByRole("region", { name: "Recommended channels" }).getByRole("heading", { level: 3 }).allInnerTexts();

test("each persona gets their own rail", async ({ page }) => {
  const problems = await open(page);
  await expect(page.getByRole("heading", { name: "Who's watching?" })).toBeVisible();
  await screenshot(page, "viewer-personas");

  await signIn(page, "Dana Reyes");
  await expect.poll(() => railChannels(page)).toEqual(["fast-demo", "fast-anim"]);
  await expect(page.getByText(/You like sci-fi/).first()).toBeVisible();
  await screenshot(page, "viewer-rail");

  await page.goto("/?mock#/personas");
  await signIn(page, "Luis Moreno");
  await expect.poll(() => railChannels(page)).toEqual(["fast-anim", "fast-demo"]);
  await expect(page.getByRole("region", { name: "Up next for you" }).getByText("Elephants Dream").first()).toBeVisible();
  expect(problems).toEqual([]);
});

test("a light-tier viewer gets one ad and slate, and the panel says why", async ({ page }) => {
  const problems = await open(page);
  await signIn(page, "Dana Reyes");
  await page.getByRole("link", { name: "Watch fast-demo" }).click();
  await expect(page.getByText(/Ticket 01MOCKCID/)).toBeVisible();
  await expect(page.getByText("Ad 1")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("Slate", { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("1 ad + 0:04 of slate")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("light", { exact: true })).toBeVisible();
  await expect(page.getByText("Light ad load: at most 1 ad, only bids above $20.00 CPM").first()).toBeVisible();
  await screenshot(page, "viewer-watch-light");
  expect(problems).toEqual([]);
});

test("a standard-tier viewer gets the full pod on the same channel", async ({ page }) => {
  const problems = await open(page);
  await signIn(page, "Marcus Hale");
  await page.getByRole("link", { name: "Watch fast-demo" }).click();
  await expect(page.getByText("Ad 2")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/^2 ads$/)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("standard", { exact: true })).toBeVisible();
  await expect(page.getByText("Standard ad load: every slot at the market floor").first()).toBeVisible();
  expect(problems).toEqual([]);
});

test("a guest plays on a guest ticket and sees no decisions", async ({ page }) => {
  const problems = await open(page);
  await page.getByRole("button", { name: "Watch as Guest" }).click();
  await expect(page.getByRole("heading", { name: "Channels on now" })).toBeVisible();
  await page.getByRole("link", { name: "Watch fast-demo" }).click();
  await expect(page.getByText(/Ticket 01MOCKGUEST/)).toBeVisible();
  await expect(page.getByText(/A guest's session is CONTEXTUAL/)).toBeVisible();
  expect(problems).toEqual([]);
});

test("a wrong password is refused", async ({ page }) => {
  const problems = await open(page);
  await page.getByRole("button", { name: "Watch as Sam Lee" }).click();
  const signInButton = page.getByRole("dialog").getByRole("button", { name: "Sign in" });
  await expect(signInButton).toBeDisabled();
  await page.getByRole("textbox", { name: "Password" }).fill("wrong");
  await signInButton.click();
  await expect(page.getByText("Incorrect username or password.")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Who's watching?" })).toBeVisible();
  expect(problems).toEqual([]);
});

test("a viewer without a consent is asked for one, and opting out changes their mode", async ({ page }) => {
  const problems = await open(page);
  await signIn(page, "Priya Shah");
  await expect(page.getByText("Choose your privacy settings")).toBeVisible();
  await page.getByRole("link", { name: "Privacy choices" }).first().click();
  await expect(page.getByText(/Your breaks now: SAFE/)).toBeVisible();
  await page.getByText("Do not sell or share my personal information").click();
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText(/Your breaks now: CONTEXTUAL/)).toBeVisible();
  await screenshot(page, "viewer-privacy");
  await page.getByRole("link", { name: "Back" }).click();
  await expect(page.getByText("Choose your privacy settings")).toBeHidden();
  expect(problems).toEqual([]);
});
