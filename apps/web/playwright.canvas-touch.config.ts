import { defineConfig } from "@playwright/test";
import scale from "./playwright.canvas-scale.config";
export default defineConfig({
  ...scale,
  testMatch: "**/canvas-touch-preview.spec.ts",
  timeout: 120_000,
  outputDir: "../../tmp/lumen-core-ui-joint/scale-touch-regression-artifacts",
  projects: scale.projects?.filter((project) => project.name === "ux-webkit-reduced"),
});
