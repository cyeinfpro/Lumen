import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "admin-invites.spec.ts",
  workers: 1,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: "list",
  outputDir: "test-results/invites",
  use: {
    baseURL: "http://127.0.0.1:3198",
    serviceWorkers: "block",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop-chromium", use: { ...devices["Desktop Chrome"], colorScheme: "light" } },
    { name: "mobile-webkit", use: { ...devices["iPhone 13"], colorScheme: "dark" } },
  ],
  webServer: {
    command: "NEXT_DIST_DIR=.next-e2e ./node_modules/.bin/next dev -H 127.0.0.1 -p 3198",
    url: "http://127.0.0.1:3198/healthz",
    reuseExistingServer: false,
    timeout: 120_000,
    env: { LUMEN_BACKEND_URL: "http://127.0.0.1:9", NEXT_PUBLIC_API_BASE: "/api" },
  },
});
