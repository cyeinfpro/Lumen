import { defineConfig } from "@playwright/test";
import ux from "./playwright.ux.config";

// Local validation only: use already installed engines, without downloading or
// relabelling Playwright revisions. Engine/driver versions are recorded in QA.
const chromium = process.env.LUMEN_QA_CHROMIUM;
const webkit = process.env.LUMEN_QA_WEBKIT;
if (!chromium || !webkit) throw new Error("Existing browser executable paths are required");
export default defineConfig({
  ...ux,
  timeout: 120_000,
  outputDir: "test-results-lumen-ux",
  projects: (ux.projects ?? []).map((project) => ({
    ...project,
    use: {
      ...project.use,
      launchOptions: { executablePath: project.use?.browserName === "webkit" ? webkit : chromium },
    },
  })),
  webServer: ux.webServer && !Array.isArray(ux.webServer) ? {
    ...ux.webServer,
    stdout: "pipe",
    stderr: "pipe",
    env: { ...ux.webServer.env, NEXT_TELEMETRY_DISABLED: "1" },
  } : ux.webServer,
});
