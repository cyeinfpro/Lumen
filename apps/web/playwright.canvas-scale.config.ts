import { defineConfig } from "@playwright/test";
import local from "./playwright.lumen-ux-local.config";
// Dedicated artifacts; no video recording or trace overhead in timing samples.
export default defineConfig({
  ...local,
  testMatch: "**/canvas-scale-performance.spec.ts",
  // Accommodates all repeated actions and artifact capture; no speed SLA.
  timeout: 360_000, retries: 0, workers: 1,
  outputDir: "../../tmp/lumen-core-ui-joint/scale-playwright",
  use: { ...local.use, actionTimeout: 15_000, video: "off", trace: "off", screenshot: "only-on-failure" },
  projects: local.projects?.filter((p) => p.name !== "ux-phone-dark"),
  webServer: local.webServer && !Array.isArray(local.webServer) ? {
    ...local.webServer, command: "NEXT_DIST_DIR=.next-ux node node_modules/next/dist/bin/next dev --webpack -H 127.0.0.1 -p 3127",
  } : local.webServer,
});
