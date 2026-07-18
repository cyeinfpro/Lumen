#!/usr/bin/env node
/**
 * 扫描 apps/web/src 下 TSX 文件，报告可能 <44×44 的可点击元素。
 * 用法: node apps/web/scripts/audit-hit-area.mjs
 * 退出码 0 = 无违规；1 = 有违规
 *
 * 启发式判定：
 *   1. <button> / <a> / role="button" 元素
 *   2. 元素本身不是 <Pressable> / <MobileIconButton>
 *   3. 静态或动态 className 分支里有小于 44px 的 h-*，且无无条件 min-h-11+
 *
 * 放行：在违规行或上一行加 `// @hit-area-ok: <reason>`
 */

import {
  existsSync,
  readdirSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { createHash } from "node:crypto";
import { join, dirname, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { auditHitAreaSource } from "./jsx-quality-analysis.mjs";

const __filename = fileURLToPath(import.meta.url);
const APP_ROOT = join(dirname(__filename), "..");
const ROOT = join(APP_ROOT, "src");
const BASELINE_PATH = join(
  APP_ROOT,
  "scripts",
  "hit-area-baseline.json",
);
const SKIP_DIRS = new Set(["node_modules", ".next", "dist"]);
const TSX_EXT = /\.tsx?$/;
const updateBaseline = process.argv.includes("--update-baseline");

const violations = [];

function normalizeSnippet(value) {
  return value.trim().replace(/\s+/g, " ").slice(0, 320);
}

function stableHash(value) {
  return createHash("sha256").update(value).digest("hex").slice(0, 16);
}

function walk(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (SKIP_DIRS.has(entry.name)) continue;
    const full = join(dir, entry.name);
    if (entry.isDirectory()) walk(full);
    else if (TSX_EXT.test(entry.name)) scan(full);
  }
}

function scan(path) {
  const src = readFileSync(path, "utf8");
  for (const finding of auditHitAreaSource(path, src)) {
    const sourcePath = relative(APP_ROOT, path).replaceAll("\\", "/");
    const snippet = normalizeSnippet(finding.snippet);
    violations.push({
      ...finding,
      path: sourcePath,
      snippet,
      key: `${sourcePath}|${stableHash(`${finding.tag}|${snippet}`)}`,
    });
  }
}

walk(ROOT);

const grouped = new Map();
for (const item of violations) {
  const current = grouped.get(item.key);
  if (current) {
    current.count += 1;
    current.lines.push(item.line);
  } else {
    grouped.set(item.key, { ...item, count: 1, lines: [item.line] });
  }
}
const current = [...grouped.values()].sort((left, right) =>
  left.path.localeCompare(right.path) ||
  left.line - right.line ||
  left.key.localeCompare(right.key),
);

if (updateBaseline) {
  const payload = {
    version: 1,
    note:
      "Known hit-area debt exposed when the scanner stopped treating a file-level Pressable/MobileIconButton import as a blanket exemption.",
    findings: current.map(
      ({ key, path, tag, snippet, count }) => ({
        key,
        path,
        tag,
        snippet,
        count,
      }),
    ),
  };
  writeFileSync(BASELINE_PATH, `${JSON.stringify(payload, null, 2)}\n`);
  console.log(
    `Updated ${relative(process.cwd(), BASELINE_PATH)} with ${current.length} known fingerprints.`,
  );
  process.exit(0);
}

const baseline = existsSync(BASELINE_PATH)
  ? JSON.parse(readFileSync(BASELINE_PATH, "utf8"))
  : { version: 1, findings: [] };
if (baseline.version !== 1 || !Array.isArray(baseline.findings)) {
  throw new Error("Unsupported hit-area baseline");
}
const allowed = new Map(
  baseline.findings.map((item) => [item.key, item.count ?? 1]),
);
const newItems = current.filter(
  (item) => item.count > (allowed.get(item.key) ?? 0),
);

if (newItems.length === 0) {
  console.log(
    `✓ 命中区审计通过：无新增违规（${current.length} 个历史指纹）。`,
  );
  process.exit(0);
}

console.error(
  `✗ 发现 ${newItems.length} 个新增的可能 <44px 点击区域指纹：\n`,
);
for (const item of newItems) {
  const extra = item.count - (allowed.get(item.key) ?? 0);
  console.error(
    `  ${item.path}:${item.lines[0]} (+${extra}) → ${item.snippet.slice(0, 180)}`,
  );
}
console.error(
  "\n修复：改走 <Pressable> / <MobileIconButton>，或加 min-h-11+，或加 // @hit-area-ok 放行。",
);
process.exit(1);
