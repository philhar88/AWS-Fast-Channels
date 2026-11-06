import { defineConfig, devices } from "@playwright/test";

/**
 * Smoke tests of the production bundles under their preview CSPs (vite.config.ts), in mock mode (`?mock`): the
 * operator console on port 4173 and the viewer app (`--mode viewer`) on 4174. The web servers start in order, so
 * the viewer's preview starts once the first one's build has written both bundles.
 * `npm run screenshots` also refreshes docs/screenshots.
 */
export default defineConfig({
  testDir: "e2e",
  timeout: 90_000,
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  reporter: [["list"]],
  use: {
    ...devices["Desktop Chrome"],
    viewport: { width: 1440, height: 1000 },
    colorScheme: "light",
  },
  projects: [
    { name: "console", testMatch: "console.spec.ts", use: { baseURL: "http://localhost:4173" } },
    { name: "viewer", testMatch: "viewer.spec.ts", use: { baseURL: "http://localhost:4174" } },
  ],
  webServer: [
    {
      command: "npm run build && npx vite preview --port 4173 --strictPort",
      url: "http://localhost:4173",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      command: "npx vite preview --mode viewer --port 4174 --strictPort",
      url: "http://localhost:4174",
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
  ],
});
