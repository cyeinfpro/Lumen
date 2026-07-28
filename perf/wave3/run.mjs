#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  DEFAULT_FIXTURE,
  FIXED_THRESHOLDS,
  buildAssets,
  pageForIndex,
  summarizeScenarios,
} from "./model.mjs";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const BROWSER_RUNNER = resolve(ROOT, "perf/wave3/browser_assets.mjs");

function parseArgs(argv) {
  const options = {
    chrome: null,
    command: "suite",
    output: null,
    url: null,
  };
  if (argv[0] && !argv[0].startsWith("--")) options.command = argv.shift();
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--chrome") options.chrome = argv[++index];
    else if (value === "--output") options.output = argv[++index];
    else if (value === "--url") options.url = argv[++index];
    else throw new Error(`unknown argument: ${value}`);
  }
  return options;
}

function gitOutput(...args) {
  const result = spawnSync("git", args, {
    cwd: ROOT,
    encoding: "utf8",
  });
  if (result.status !== 0) {
    throw new Error(result.stderr || `git ${args.join(" ")} failed`);
  }
  return result.stdout.trim();
}

function browserScenario(args) {
  const result = spawnSync(
    process.execPath,
    [BROWSER_RUNNER, "--json", ...args],
    {
      cwd: ROOT,
      encoding: "utf8",
      env: process.env,
      maxBuffer: 20 * 1024 * 1024,
    },
  );
  if (result.status !== 0) {
    return {
      reason: "browser runner failed",
      status: "error",
      stderr: result.stderr.slice(-4000),
      stdout: result.stdout.slice(-4000),
    };
  }
  return JSON.parse(result.stdout);
}

function fixtureContract() {
  const assets = buildAssets();
  return {
    count: assets.length,
    pageSize: DEFAULT_FIXTURE.pageSize,
    scenarioCounts: summarizeScenarios(assets),
    search: {
      page: pageForIndex(DEFAULT_FIXTURE.searchTargetIndex),
      query: DEFAULT_FIXTURE.searchQuery,
      targetId: `asset-${DEFAULT_FIXTURE.searchTargetIndex}`,
      targetIndex: DEFAULT_FIXTURE.searchTargetIndex,
    },
    status: "modeled",
  };
}

function suite(options) {
  const common = options.chrome ? ["--chrome", options.chrome] : [];
  const legacy = browserScenario(["--mode", "legacy", ...common]);
  const targetDesktop = browserScenario(["--mode", "target", ...common]);
  const targetMobileWeak = browserScenario([
    "--mode",
    "target",
    "--mobile",
    "--save-data",
    "--effective-type",
    "3g",
    ...common,
  ]);
  const realTarget = options.url
    ? browserScenario(["--url", options.url, ...common])
    : {
        reason:
          "Set --url or LUMEN_WAVE3_ASSET_URL after Wave 3 product integration.",
        status: "gated",
      };
  return {
    branch: gitOutput("branch", "--show-current") || "(detached)",
    fixedThresholds: FIXED_THRESHOLDS,
    generatedAt: "2026-07-28T23:59:57+08:00",
    head: gitOutput("rev-parse", "HEAD"),
    invariants: [
      "The 1000 item fixture distribution is deterministic and exact.",
      "Fixed mounted DOM and prewarm thresholds are source constants, not baseline-derived.",
      "Grid binary requests and hover-triggered display requests must remain zero.",
      "Server search must find the page 20 target with only the initial page loaded.",
      "Weak-network and Save-Data behavior is measured in a real Chromium session.",
      "Synthetic fixture acceptance is not production SLO evidence.",
    ],
    scenarios: {
      fixtureContract: fixtureContract(),
      frozenCurrentModel: legacy,
      realProductAfter: realTarget,
      targetOracleDesktop: targetDesktop,
      targetOracleMobileSaveData3g: targetMobileWeak,
    },
    schemaVersion: 1,
    status: "characterized",
  };
}

const options = parseArgs(process.argv.slice(2));
if (process.env.LUMEN_WAVE3_ASSET_URL && !options.url) {
  options.url = process.env.LUMEN_WAVE3_ASSET_URL;
}
let result;
if (options.command === "contract") result = fixtureContract();
else if (options.command === "legacy") {
  result = browserScenario([
    "--mode",
    "legacy",
    ...(options.chrome ? ["--chrome", options.chrome] : []),
  ]);
} else if (options.command === "target") {
  result = browserScenario([
    "--mode",
    "target",
    ...(options.chrome ? ["--chrome", options.chrome] : []),
  ]);
} else if (options.command === "suite") result = suite(options);
else throw new Error(`unknown command: ${options.command}`);

const rendered = `${JSON.stringify(result, null, 2)}\n`;
if (options.output) await writeFile(options.output, rendered, "utf8");
process.stdout.write(rendered);

