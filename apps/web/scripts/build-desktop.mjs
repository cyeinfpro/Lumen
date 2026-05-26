import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const nextCli = join(projectRoot, "node_modules", "next", "dist", "bin", "next");

process.env.NEXT_PUBLIC_LUMEN_RUNTIME = "desktop";
process.env.LUMEN_BACKEND_URL =
  process.env.LUMEN_BACKEND_URL || "http://127.0.0.1:8000";

const result = spawnSync(process.execPath, [nextCli, "build"], {
  cwd: projectRoot,
  env: process.env,
  stdio: "inherit",
});

if (result.error) {
  console.error(result.error);
  process.exit(1);
}

process.exit(result.status ?? 1);
