import { defineConfig } from "@playwright/test";
import base from "./playwright.config";
export default defineConfig({
  ...base, testMatch: ["**/gif-editing.spec.ts"], timeout: 240_000, retries: 0,
  outputDir: "../../tmp/gif-editing-ui-20261105/browser-results",
  projects: [
    { name: "desktop-system-chrome", use: { browserName: "chromium", channel: "chrome", viewport: { width: 1440, height: 1000 } } },
    { name: "mobile-system-chrome", use: { browserName: "chromium", channel: "chrome", viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
  ],
  webServer: {
    command: "NEXT_DIST_DIR=.next-gif-editing npx --no-install next dev --webpack -H 127.0.0.1 -p 3107",
    url: "http://127.0.0.1:3107/healthz", reuseExistingServer: true, timeout: 360_000,
    env: { LUMEN_BACKEND_URL: "http://127.0.0.1:9", NEXT_PUBLIC_API_BASE: "/api" },
  },
  use: { ...base.use, actionTimeout: 15_000, navigationTimeout: 220_000, video: "off", baseURL: "http://127.0.0.1:3107", serviceWorkers: "block" },
});
