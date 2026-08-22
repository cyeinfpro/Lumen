import { spawnSync } from "node:child_process";
import { accessSync, constants } from "node:fs";

const required = [
  "PLAYWRIGHT_BASE_URL",
  "AGENT_E2E_CONTROL_URL",
  "AGENT_E2E_CONTROL_TOKEN",
  "AGENT_E2E_USER_A_EMAIL",
  "AGENT_E2E_USER_A_PASSWORD",
  "AGENT_E2E_USER_B_EMAIL",
  "AGENT_E2E_USER_B_PASSWORD",
  "AGENT_E2E_REFERENCE_IMAGE",
];

if (process.env.AGENT_FULL_STACK_E2E !== "1") {
  console.error("Refusing live Agent E2E: set AGENT_FULL_STACK_E2E=1 explicitly.");
  process.exit(2);
}
const missing = required.filter((name) => !process.env[name]?.trim());
if (missing.length > 0) {
  console.error(`Missing Agent full-stack E2E settings: ${missing.join(", ")}`);
  process.exit(2);
}
try {
  accessSync(process.env.AGENT_E2E_REFERENCE_IMAGE, constants.R_OK);
} catch {
  console.error("AGENT_E2E_REFERENCE_IMAGE is not readable.");
  process.exit(2);
}

const result = spawnSync(
  process.platform === "win32" ? "npx.cmd" : "npx",
  ["playwright", "test", "e2e/agent-live.spec.ts", "--project=desktop-light"],
  { stdio: "inherit", env: process.env },
);
process.exit(result.status ?? 1);
