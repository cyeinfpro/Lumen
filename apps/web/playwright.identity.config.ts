import { defineConfig, devices } from "@playwright/test";
import baseConfig from "./playwright.config";

// Exercise native-picker return events in both browser engines used by the
// affected upload flow. API responses are fixtures; no production data is used.
export default defineConfig({
  ...baseConfig,
  testMatch: "composer-reference-upload.spec.ts",
  projects: [
    {
      name: "identity-chromium",
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "identity-webkit-iphone",
      use: { ...devices["iPhone 13"], browserName: "webkit" },
    },
  ],
});
