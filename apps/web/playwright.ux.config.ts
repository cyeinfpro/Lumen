import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

const externalBaseURL = process.env.PLAYWRIGHT_BASE_URL;

// Isolated fixture-backed browsers; never attach to an owner's interactive tab.
// Webpack matches this project's build command and keeps the audit independent
// of the existing development server on port 3000.
export default defineConfig({
  ...base,
  timeout: 60_000,
  retries: 0,
  outputDir: "test-results-ux",
  use: { ...base.use, baseURL: externalBaseURL || "http://127.0.0.1:3127" },
  webServer: externalBaseURL ? undefined : {
    command: "NEXT_DIST_DIR=.next-ux node node_modules/next/dist/bin/next dev --webpack -H 127.0.0.1 -p 3127",
    // Wait for an actual page bundle, not just the lightweight health route.
    url: "http://127.0.0.1:3127/login",
    reuseExistingServer: false,
    timeout: 300_000,
    env: { LUMEN_BACKEND_URL: "http://127.0.0.1:9", NEXT_PUBLIC_API_BASE: "/api" },
  },
  projects: [
    { name: "ux-desktop-light", use: { browserName: "chromium", viewport: { width: 1440, height: 900 }, colorScheme: "light" } },
    { name: "ux-phone-dark", use: { browserName: "chromium", viewport: { width: 375, height: 812 }, isMobile: true, hasTouch: true, colorScheme: "dark" } },
    { name: "ux-webkit-reduced", use: { browserName: "webkit", viewport: { width: 375, height: 812 }, isMobile: true, hasTouch: true, colorScheme: "dark", contextOptions: { reducedMotion: "reduce" } } },
  ],
});
