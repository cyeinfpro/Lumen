import { defineConfig } from "@playwright/test";
import ux from "./playwright.ux.config";

// Keep this focused suite isolated from the interactive development server.
export default defineConfig({
  ...ux,
  workers: 1,
  // Cold Next dev compilation counts toward test time; assertion limits stay unchanged.
  timeout: 120_000,
  webServer: ux.webServer && !Array.isArray(ux.webServer)
    ? { ...ux.webServer, url: ux.use?.baseURL ?? ux.webServer.url, stdout: "pipe", stderr: "pipe" }
    : ux.webServer,
  outputDir: "test-results-design",
  projects: [
    ...(ux.projects ?? []),
    { name: "design-phone-320-light", use: { browserName: "chromium", viewport: { width: 320, height: 700 }, colorScheme: "light", hasTouch: true, isMobile: true } },
    { name: "design-tablet-light", use: { browserName: "chromium", viewport: { width: 768, height: 1024 }, colorScheme: "light", hasTouch: true } },
  ],
});
