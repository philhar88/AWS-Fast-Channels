import { defineConfig, devices } from "@playwright/test";

/**
 * Smoke tests of the production bundle under the preview CSP (vite.config.ts), in mock mode (`?mock`).
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
    baseURL: "http://localhost:4173",
    viewport: { width: 1440, height: 1000 },
    colorScheme: "light",
  },
  webServer: {
    command: "npm run build && npx vite preview --port 4173 --strictPort",
    url: "http://localhost:4173",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
