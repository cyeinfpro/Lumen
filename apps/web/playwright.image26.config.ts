import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

export default defineConfig({
  ...base,
  testMatch: ["**/image26-markup.spec.ts", "**/image-models.spec.ts"],
  timeout: 90_000,
  outputDir: "../../tmp/image-controls-26-20261105/browser-results",
  projects: process.env.IMAGE26_SYSTEM_CHROME === "1" ? [
    { name: "desktop-system-chrome", use: { browserName: "chromium", channel: "chrome", viewport: { width: 1440, height: 1000 } } },
    { name: "mobile-system-chrome", use: { browserName: "chromium", channel: "chrome", viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
  ] : [
    { name: "desktop-chromium", use: { browserName: "chromium", viewport: { width: 1440, height: 1000 } } },
    { name: "mobile-webkit", use: { browserName: "webkit", viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
  ],
  webServer: {
    command: "NEXT_DIST_DIR=.next-image26 npx --no-install next dev --webpack -H 127.0.0.1 -p 3106",
    url: "http://127.0.0.1:3106/healthz",
    reuseExistingServer: false,
    timeout: 360_000,
    env: { LUMEN_BACKEND_URL: "http://127.0.0.1:9", NEXT_PUBLIC_API_BASE: "/api" },
  },
  use: { ...base.use, video: "off", baseURL: "http://127.0.0.1:3106", serviceWorkers: "block" },
});
