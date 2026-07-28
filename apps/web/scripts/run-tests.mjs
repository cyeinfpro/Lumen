#!/usr/bin/env node

import {
  existsSync,
  readdirSync,
  realpathSync,
  statSync,
} from "node:fs";
import {
  dirname,
  isAbsolute,
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

function isInsideRoot(root, candidate) {
  const relativePath = relative(root, candidate);
  return (
    relativePath === ""
    || (!relativePath.startsWith("..") && !isAbsolute(relativePath))
  );
}

function normalizeExplicitTestFile(input, webRoot) {
  if (input.split(/[\\/]+/).includes("..")) {
    throw new Error(`Test path must not contain '..': ${input}`);
  }
  const resolvedRoot = resolve(webRoot);
  const resolvedPath = isAbsolute(input)
    ? resolve(input)
    : resolve(resolvedRoot, input);
  if (!isInsideRoot(resolvedRoot, resolvedPath)) {
    throw new Error(`Test file must be inside the Web root: ${input}`);
  }
  if (!existsSync(resolvedPath) || !statSync(resolvedPath).isFile()) {
    throw new Error(`Test file does not exist: ${input}`);
  }
  if (!TEST_FILE_RE.test(resolvedPath)) {
    throw new Error(`Expected a test/spec file: ${input}`);
  }
  const realRoot = realpathSync(resolvedRoot);
  const realPath = realpathSync(resolvedPath);
  if (!isInsideRoot(realRoot, realPath)) {
    throw new Error(`Test file must be inside the Web root: ${input}`);
  }
  return relative(resolvedRoot, resolvedPath).split("\\").join("/");
}

export function selectTestFiles(args, webRoot = DEFAULT_WEB_ROOT) {
  if (args.length === 0) return discoverTestFiles(webRoot);
  return [
    ...new Set(args.map((input) => normalizeExplicitTestFile(input, webRoot))),
  ];
}

function run(args = process.argv.slice(2)) {
  let testFiles;
  try {
    testFiles = selectTestFiles(args);
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
    return;
  }
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
