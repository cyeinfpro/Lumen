#!/usr/bin/env node

import { readFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const appRoot = join(scriptDir, "..");
const globalsPath = join(appRoot, "src", "app", "globals.css");
const topNavPath = join(
  appRoot,
  "src",
  "components",
  "ui",
  "shell",
  "DesktopTopNav.tsx",
);

const globals = readFileSync(globalsPath, "utf8");
const topNav = readFileSync(topNavPath, "utf8");
const expected = {
  "--appbar-h": "56px",
  "--mobile-topbar-h": "56px",
  "--mobile-tabbar-h": "56px",
  "--sidebar-rail-w": "64px",
  "--sidebar-panel-w": "248px",
  "--content-text": "800px",
  "--content-composer": "880px",
  "--content-media": "1160px",
  "--content-workbench": "1440px",
};

const errors = [];
for (const [token, value] of Object.entries(expected)) {
  const escapedToken = token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const escapedValue = value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const pattern = new RegExp(`${escapedToken}\\s*:\\s*${escapedValue}\\s*;`);
  if (!pattern.test(globals)) {
    errors.push(`${token} must be ${value}`);
  }
}

if (!/h-\[var\(--appbar-h\)\]/.test(topNav)) {
  errors.push("DesktopTopNav must consume --appbar-h");
}
if (/h-\[52px\]/.test(topNav)) {
  errors.push("DesktopTopNav must not restore the legacy 52px height");
}

if (errors.length > 0) {
  console.error("Layout contract failed:");
  for (const error of errors) console.error(`- ${error}`);
  process.exit(1);
}

console.log(
  `Layout contract passed: ${Object.keys(expected).length} tokens and ${relative(
    appRoot,
    topNavPath,
  )}.`,
);
