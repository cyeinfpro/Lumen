#!/usr/bin/env node

import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import vm from "node:vm";
import zlib from "node:zlib";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const appRoot = path.resolve(scriptDir, "..");
const defaultBudgetPath = path.join(scriptDir, "bundle-budget.json");

function resolveDistDir(root = appRoot) {
  const configured = process.env.NEXT_DIST_DIR?.trim() || ".next";
  if (
    !/^[A-Za-z0-9._-]+$/u.test(configured) ||
    configured === "." ||
    configured === ".."
  ) {
    throw new Error(`Invalid NEXT_DIST_DIR: ${configured}`);
  }
  return path.join(root, configured);
}

function formatKiB(bytes) {
  return `${(bytes / 1024).toFixed(1)} KiB`;
}

function loadJson(filePath) {
  return JSON.parse(readFileSync(filePath, "utf8"));
}

function loadClientReferenceManifest(manifestPath, routeKey) {
  const context = { globalThis: {} };
  context.globalThis.globalThis = context.globalThis;
  vm.runInNewContext(readFileSync(manifestPath, "utf8"), context);
  const registry = context.globalThis.__RSC_MANIFEST ?? {};
  return registry[routeKey] ?? Object.values(registry)[0] ?? null;
}

function normalizeChunkRelPath(chunk) {
  return String(chunk).replace(/^\/_next\//, "");
}

function routeEntryPrefixes(serverJsRel) {
  const rawBase = `static/chunks/${serverJsRel.replace(/\.js$/, "-")}`;
  const encodedBase = rawBase
    .split("/")
    .map((segment, idx, arr) =>
      idx === arr.length - 1 ? segment : encodeURIComponent(segment),
    )
    .join("/");
  return [...new Set([rawBase, encodedBase])];
}

function collectRouteInitialChunks(buildManifest, crm, serverJsRel) {
  const chunks = new Set(
    (buildManifest.rootMainFiles ?? [])
      .map(normalizeChunkRelPath)
      .filter((item) => item.endsWith(".js")),
  );
  if (!crm?.clientModules) return [...chunks];

  const prefixes = routeEntryPrefixes(serverJsRel);
  for (const modInfo of Object.values(crm.clientModules)) {
    const jsChunks = (modInfo?.chunks ?? [])
      .filter((item) => typeof item === "string" && item.endsWith(".js"))
      .map(normalizeChunkRelPath);
    const belongsToInitialRoute = jsChunks.some(
      (chunk) =>
        chunk.startsWith("static/chunks/app/layout-") ||
        prefixes.some((prefix) => chunk.startsWith(prefix)),
    );
    if (belongsToInitialRoute) {
      for (const chunk of jsChunks) chunks.add(chunk);
    }
  }
  return [...chunks];
}

function collectLayoutInitialChunks(buildManifest, crm) {
  const chunks = new Set(
    (buildManifest.rootMainFiles ?? [])
      .map(normalizeChunkRelPath)
      .filter((item) => item.endsWith(".js")),
  );
  if (!crm?.clientModules) return [...chunks];
  for (const modInfo of Object.values(crm.clientModules)) {
    const jsChunks = (modInfo?.chunks ?? [])
      .filter((item) => typeof item === "string" && item.endsWith(".js"))
      .map(normalizeChunkRelPath);
    if (jsChunks.some((chunk) => chunk.startsWith("static/chunks/app/layout-"))) {
      for (const chunk of jsChunks) chunks.add(chunk);
    }
  }
  return [...chunks];
}

export function auditNextBundleBudget({
  webRoot = appRoot,
  distDir = resolveDistDir(webRoot),
  budgetPath = defaultBudgetPath,
} = {}) {
  const config = loadJson(budgetPath);
  const buildManifestPath = path.join(distDir, "build-manifest.json");
  const appPathsManifestPath = path.join(distDir, "server", "app-paths-manifest.json");
  const loadableManifestPath = path.join(distDir, "react-loadable-manifest.json");

  if (!existsSync(buildManifestPath) || !existsSync(appPathsManifestPath)) {
    throw new Error(
      `Next build output not found at ${distDir}. Run "npm run build" before "npm run check:bundle-budget".`,
    );
  }

  const buildManifest = loadJson(buildManifestPath);
  const appPathsManifest = loadJson(appPathsManifestPath);
  const loadableManifest = existsSync(loadableManifestPath)
    ? loadJson(loadableManifestPath)
    : {};

  const chunkCache = new Map();
  const inspectChunk = (relPath) => {
    const cached = chunkCache.get(relPath);
    if (cached) return cached;
    const fullPath = path.join(distDir, relPath);
    if (!existsSync(fullPath)) {
      throw new Error(`Referenced client chunk missing on disk: ${relPath}`);
    }
    const rawBuffer = readFileSync(fullPath);
    const gzipBuffer = zlib.gzipSync(rawBuffer);
    const text = rawBuffer.toString("utf8");
    const entry = {
      relPath,
      rawBytes: rawBuffer.length,
      gzipBytes: gzipBuffer.length,
      text,
    };
    chunkCache.set(relPath, entry);
    return entry;
  };

  const sumChunkSet = (relPaths) => {
    let rawBytes = 0;
    let gzipBytes = 0;
    for (const relPath of new Set(relPaths)) {
      const info = inspectChunk(relPath);
      rawBytes += info.rawBytes;
      gzipBytes += info.gzipBytes;
    }
    return { chunkCount: new Set(relPaths).size, rawBytes, gzipBytes };
  };

  const rootMainChunks = (buildManifest.rootMainFiles ?? [])
    .map(normalizeChunkRelPath)
    .filter((item) => item.endsWith(".js"));
  const rootMainStats = sumChunkSet(rootMainChunks);

  const rootPageServerJs = appPathsManifest["/page"];
  const rootPageCrmPath = rootPageServerJs
    ? path.join(
        distDir,
        "server",
        rootPageServerJs.replace(/\.js$/, "_client-reference-manifest.js"),
      )
    : null;
  const rootPageCrm =
    rootPageCrmPath && existsSync(rootPageCrmPath)
      ? loadClientReferenceManifest(rootPageCrmPath, "/page")
      : null;

  const layoutChunks = collectLayoutInitialChunks(buildManifest, rootPageCrm);
  const layoutStats = sumChunkSet(layoutChunks);

  const routeReports = {};
  const violations = [];
  const compiledForbidden = (config.forbiddenInitialChunkPatterns ?? []).map(
    (entry) => ({
      ...entry,
      regex: new RegExp(entry.pattern),
    }),
  );

  for (const routeKey of config.auditedInitialRoutes ?? []) {
    const serverJs = appPathsManifest[routeKey];
    if (!serverJs) {
      violations.push(`Audited route "${routeKey}" is missing from app-paths-manifest.json.`);
      continue;
    }
    const crmPath = path.join(
      distDir,
      "server",
      serverJs.replace(/\.js$/, "_client-reference-manifest.js"),
    );
    if (!existsSync(crmPath)) {
      violations.push(`Client reference manifest missing for "${routeKey}": ${crmPath}`);
      continue;
    }
    const crm = loadClientReferenceManifest(crmPath, routeKey);
    const chunks = collectRouteInitialChunks(buildManifest, crm, serverJs);
    const stats = sumChunkSet(chunks);
    routeReports[routeKey] = { ...stats, chunks };

    for (const chunk of chunks) {
      const { text } = inspectChunk(chunk);
      for (const rule of compiledForbidden) {
        if (rule.regex.test(text)) {
          violations.push(
            `Forbidden initial-load dependency [${rule.id}] leaked into "${routeKey}" via ${chunk} (${rule.description}).`,
          );
        }
      }
    }
  }

  const budgetChecks = [
    {
      name: "sharedRootMainGzipBytes",
      actual: rootMainStats.gzipBytes,
      limit: config.budgets.sharedRootMainGzipBytes,
    },
    {
      name: "sharedLayoutFirstLoadGzipBytes",
      actual: layoutStats.gzipBytes,
      limit: config.budgets.sharedLayoutFirstLoadGzipBytes,
    },
    {
      name: "chatRouteFirstLoadGzipBytes",
      actual: routeReports["/page"]?.gzipBytes ?? Number.POSITIVE_INFINITY,
      limit: config.budgets.chatRouteFirstLoadGzipBytes,
    },
    {
      name: "agentRouteFirstLoadGzipBytes",
      actual: routeReports["/agent/page"]?.gzipBytes ?? Number.POSITIVE_INFINITY,
      limit: config.budgets.agentRouteFirstLoadGzipBytes,
    },
  ];

  for (const check of budgetChecks) {
    if (check.actual > check.limit) {
      violations.push(
        `Bundle budget exceeded for ${check.name}: ${check.actual} B (${formatKiB(check.actual)}) > limit ${check.limit} B (${formatKiB(check.limit)}).`,
      );
    }
  }

  const loadableKeys = Object.keys(loadableManifest);
  for (const requiredBoundary of config.requiredDynamicBoundaries ?? []) {
    if (!loadableKeys.some((key) => key.includes(requiredBoundary))) {
      violations.push(
        `Expected dynamic split boundary "${requiredBoundary}" was not found in react-loadable-manifest.json.`,
      );
    }
  }

  const lazyInpaintPath = path.join(
    webRoot,
    "src/components/ui/inpaint/LazyInpaintModal.tsx",
  );
  if (existsSync(lazyInpaintPath)) {
    const lazyInpaintSource = readFileSync(lazyInpaintPath, "utf8");
    if (!/if\s*\(\s*!open\s*\)\s*return\s+null/.test(lazyInpaintSource)) {
      violations.push(
        "LazyInpaintModal.tsx must gate <InpaintModalImpl /> behind open state so konva is not fetched on initial mount.",
      );
    }
  }

  return {
    ok: violations.length === 0,
    rootMainStats,
    layoutStats,
    routeReports,
    budgetChecks,
    violations,
  };
}

function main() {
  const report = auditNextBundleBudget();
  console.log("[bundle-budget] Client First-Load Gzip Summary:");
  for (const item of report.budgetChecks) {
    const headroom = item.limit - item.actual;
    console.log(
      `  - ${item.name}: ${item.actual} B (${formatKiB(item.actual)}) / ${item.limit} B (${formatKiB(item.limit)}) [headroom: ${formatKiB(headroom)}]`,
    );
  }
  for (const [routeKey, stats] of Object.entries(report.routeReports)) {
    console.log(
      `  - route ${routeKey}: ${stats.gzipBytes} B (${formatKiB(stats.gzipBytes)}) across ${stats.chunkCount} chunks`,
    );
  }
  if (!report.ok) {
    console.error("\n[bundle-budget] FAILED:");
    for (const violation of report.violations) {
      console.error(`  * ${violation}`);
    }
    process.exitCode = 1;
    return;
  }
  console.log("[bundle-budget] OK — all gzip budgets and dynamic isolation boundaries verified.");
}

const entryHref = process.argv[1]
  ? pathToFileURL(path.resolve(process.argv[1])).href
  : null;
if (entryHref === import.meta.url) {
  main();
}
