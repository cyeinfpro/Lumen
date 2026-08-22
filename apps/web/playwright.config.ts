import { defineConfig } from "@playwright/test";

const viewports = [
  { name: "phone-320-light", width: 320, height: 700, colorScheme: "light" },
  { name: "phone-375-dark", width: 375, height: 812, colorScheme: "dark" },
  { name: "phone-landscape-dark", width: 700, height: 320, colorScheme: "dark" },
  { name: "tablet-portrait-light", width: 768, height: 1024, colorScheme: "light" },
  { name: "tablet-landscape-dark", width: 1024, height: 768, colorScheme: "dark" },
  { name: "desktop-light", width: 1440, height: 900, colorScheme: "light" },
] as const;
const externalBaseURL = process.env.PLAYWRIGHT_BASE_URL?.trim() || null;
const fullStackAgentE2E = process.env.AGENT_FULL_STACK_E2E === "1";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 45_000,
  expect: { timeout: 8_000 },
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: externalBaseURL ?? "http://127.0.0.1:3100",
    serviceWorkers: "block",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: viewports.map((viewport) => ({
    name: viewport.name,
    testIgnore: fullStackAgentE2E ? [] : ["**/agent-live.spec.ts"],
    use: {
      viewport: { width: viewport.width, height: viewport.height },
      colorScheme: viewport.colorScheme,
    },
  })),
  webServer: externalBaseURL
    ? undefined
    : {
        command: "NEXT_DIST_DIR=.next-e2e npx next dev -H 127.0.0.1 -p 3100",
        url: "http://127.0.0.1:3100/healthz",
        reuseExistingServer: !process.env.CI,
        timeout: 120_000,
        env: {
          LUMEN_BACKEND_URL: "http://127.0.0.1:9",
          NEXT_PUBLIC_API_BASE: "/api",
        },
      },
});
