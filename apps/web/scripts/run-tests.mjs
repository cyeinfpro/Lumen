#!/usr/bin/env node

import {
  existsSync,
  readdirSync,
} from "node:fs";
import {
  dirname,
  join,
  relative,
  resolve,
} from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const DEFAULT_WEB_ROOT = resolve(SCRIPT_DIR, "..");
const TEST_ROOTS = ["__tests__", "src"];
const TEST_FILE_RE = /\.(?:test|spec)\.[cm]?[jt]sx?$/;
const SKIP_DIRS = new Set([".next", "node_modules"]);

function walkTests(root, directory, files) {
  if (!existsSync(directory)) return;
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    if (entry.isDirectory() && SKIP_DIRS.has(entry.name)) continue;
    const fullPath = join(directory, entry.name);
    if (entry.isDirectory()) {
      walkTests(root, fullPath, files);
    } else if (entry.isFile() && TEST_FILE_RE.test(entry.name)) {
      files.push(relative(root, fullPath).split("\\").join("/"));
    }
  }
}

export function discoverTestFiles(webRoot = DEFAULT_WEB_ROOT) {
  const files = [];
  for (const testRoot of TEST_ROOTS) {
    walkTests(webRoot, join(webRoot, testRoot), files);
  }
  return files.sort();
}

function run() {
  const testFiles = discoverTestFiles();
  if (testFiles.length === 0) {
    console.error("No frontend test files were discovered.");
    process.exitCode = 1;
    return;
  }

  const result = spawnSync(process.execPath, ["--test", ...testFiles], {
    cwd: DEFAULT_WEB_ROOT,
    stdio: "inherit",
  });
  if (result.error) throw result.error;
  process.exitCode = result.status ?? 1;
}

const entryPath = process.argv[1]
  ? pathToFileURL(resolve(process.argv[1])).href
  : null;
if (entryPath === import.meta.url) run();
