#!/usr/bin/env node

import {
  existsSync,
  readdirSync,
  readFileSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { createHash } from "node:crypto";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import {
  getGitChangeScope,
  repoRelativePath,
} from "./git-change-scope.mjs";
import { findMobileDialogIssues } from "./jsx-quality-analysis.mjs";

const __filename = fileURLToPath(import.meta.url);
const APP_ROOT = join(dirname(__filename), "..");
const SRC_ROOT = join(APP_ROOT, "src");
const BASELINE_PATH = join(APP_ROOT, "scripts", "ui-governance-baseline.json");

const args = new Set(process.argv.slice(2));
const updateBaseline = args.has("--update-baseline");
const verbose = args.has("--verbose");

const SOURCE_EXT = /\.(?:ts|tsx|js|jsx)$/;
const SKIP_DIRS = new Set(["node_modules", ".next", "dist", "build"]);

const DARK_TOKEN_RE =
  /\b(?:bg-neutral-9(?:00|50)(?:\/(?:\d+|\[[^\]]+\]))?|bg-black(?:\/(?:\d+|\[[^\]]+\]))?|text-white(?:\/(?:\d+|\[[^\]]+\]))?|hover:text-white(?:\/(?:\d+|\[[^\]]+\]))?|text-neutral-(?:100|200)(?:\/(?:\d+|\[[^\]]+\]))?|border-white(?:\/(?:\d+|\[[^\]]+\]))?)\b/g;

const LIVE_REGION_RE = /\b(?:role=(?:"|')?(?:alert|status)|aria-live=)/;

const files = [];
const findings = [];

function walk(dir) {
  for (const name of readdirSync(dir)) {
    if (SKIP_DIRS.has(name)) continue;
    const full = join(dir, name);
    const st = statSync(full);
    if (st.isDirectory()) walk(full);
    else if (SOURCE_EXT.test(name)) files.push(full);
  }
}

function normalizeSnippet(value) {
  return value.trim().replace(/\s+/g, " ").slice(0, 220);
}

function stableHash(value) {
  return createHash("sha256").update(value).digest("hex").slice(0, 16);
}

function addFinding(rule, path, line, message, snippet) {
  const normalized = normalizeSnippet(snippet);
  const key = `${rule}|${path}|${stableHash(`${message}|${normalized}`)}`;
  findings.push({ key, rule, path, line, message, snippet: normalized });
}

function lineContext(lines, index, radius = 4) {
  const start = Math.max(0, index - radius);
  const end = Math.min(lines.length, index + radius + 1);
  return lines.slice(start, end).join("\n");
}

function isCommentOnly(line) {
  const t = line.trim();
  return t.startsWith("//") || t.startsWith("*") || t.startsWith("/*");
}

function hasInlineAllow(context, kind) {
  const allow = context.match(/@ui-governance-allow\s+([a-z,-]+)/);
  if (!allow) return false;
  return allow[1].split(",").includes(kind);
}

function isCodeContext(path, context) {
  return (
    /(?:^|\/)(?:Markdown|MarkdownPreview)\.tsx$/i.test(path) ||
    /\b(?:pre|code|hljs|highlight|font-mono|terminal|log)\b/i.test(context)
  );
}

function isMediaPath(path) {
  return /(?:lightbox|gallery|image|inpaint|maskcanvas|candidatecard|generationtile|viewportimage|premiumimagecard|attachmenttray|modelibrary|poster|share\/\[token\]\/sharecontentclient|apparelworkflow|taskitem)/i.test(
    path,
  );
}

function isMediaContext(path, context) {
  return (
    isMediaPath(path) ||
    /\b(?:img|image|photo|picture|thumbnail|canvas|mask|preview|lightbox|media|tile|poster)\b/i.test(
      context,
    ) ||
    /\bmix-blend-difference\b/.test(context)
  );
}

function isScrimContext(context, token) {
  return (
    token.startsWith("bg-black") &&
    /\b(?:fixed|absolute|backdrop:)\b/.test(context) &&
    /\b(?:inset-0|backdrop|scrim|z-\[var\(--z-|z-50|z-40|z-30)\b/.test(context)
  );
}

function isDangerOnColorContext(context, token) {
  if (!/(?:text-white|hover:text-white)/.test(token)) return false;
  return /\b(?:bg-danger|bg-\[var\(--danger\)\]|bg-\[var\(--success\)\]|bg-success|bg-\[var\(--accent\)\]|bg-accent|bg-\[var\(--color-lumen-amber\)\]|bg-warning|bg-\[var\(--warning\)\])\b/.test(
    context,
  );
}

function allowedDarkReason(path, context, token) {
  if (hasInlineAllow(context, "media")) return "media";
  if (hasInlineAllow(context, "code")) return "code";
  if (hasInlineAllow(context, "danger")) return "danger";
  if (hasInlineAllow(context, "scrim")) return "scrim";
  if (isDangerOnColorContext(context, token)) return "danger";
  if (isScrimContext(context, token)) return "scrim";
  if (isCodeContext(path, context)) return "code";
  if (isMediaContext(path, context)) return "media";
  return null;
}

function scanDarkUtilities(path, lines) {
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (isCommentOnly(line)) continue;
    const context = lineContext(lines, i);
    DARK_TOKEN_RE.lastIndex = 0;
    let match;
    while ((match = DARK_TOKEN_RE.exec(line)) !== null) {
      const token = match[0];
      if (allowedDarkReason(path, context, token)) continue;
      addFinding(
        "hardcoded-dark",
        path,
        i + 1,
        `Hard-coded dark utility "${token}" must be semantic token UI or an allowlisted media/code/danger/scrim context.`,
        line,
      );
    }
  }
}

function scanMobileDialogs(path, src) {
  for (const issue of findMobileDialogIssues(path, src)) {
    addFinding(
      issue.rule,
      path,
      issue.line,
      issue.message,
      issue.snippet,
    );
  }
}

function scanErrorLiveRegions(path, lines) {
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (isCommentOnly(line)) continue;
    const context = lineContext(lines, i, 5);
    const lineHasDangerStyle =
      /\b(?:text-danger|bg-danger-soft|border-danger-border|var\(--danger(?:-soft|-fg)?\))\b/.test(
        line,
      );
    const lineHasErrorSignal =
      /\b(?:error|Error|failed|failure|失败|异常|错误|失效|不正确|未填)\b/.test(line);
    const looksLikeInlineError =
      lineHasDangerStyle && lineHasErrorSignal;

    const conditionalError = /\{[^}\n]*(?:error|Error)[^}\n]*\?\s*\(/.test(line);

    if (!(looksLikeInlineError || conditionalError)) continue;
    if (LIVE_REGION_RE.test(context)) continue;
    if (/(?:Badge|Button|IconButton|Toast)\.tsx$/.test(path)) continue;

    addFinding(
      "aria-live-error",
      path,
      i + 1,
      "Visible inline error/status content should include role=\"alert\"/role=\"status\" or aria-live nearby.",
      line,
    );
  }
}

function scanSharedA11yContracts(root) {
  const contracts = [
    {
      path: "src/components/ui/primitives/Toast.tsx",
      test: (src) =>
        /role=\{item\.tone === "error" \|\| item\.tone === "warning" \? "alert" : "status"\}/.test(
          src,
        ) &&
        /aria-live=\{item\.tone === "error" \|\| item\.tone === "warning" \? "assertive" : "polite"\}/.test(
          src,
        ),
      message: "Global Toast must announce error/warning toasts assertively and other toasts politely.",
    },
    {
      path: "src/components/ui/primitives/mobile/Toast.tsx",
      test: (src) =>
        /if \(kind === "danger"\) toast\.error\(title\)/.test(src) &&
        /else if \(kind === "warning"\) toast\.warning\(title\)/.test(src) &&
        /export function MobileToastViewport\(\) \{\s+return null;\s+\}/.test(src),
      message:
        "MobileToast must forward danger/warning tones to the accessible global Toast and avoid a duplicate viewport.",
    },
    {
      path: "src/components/ui/primitives/ErrorState.tsx",
      test: (src) => /role="alert"/.test(src),
      message: "ErrorState must expose role=\"alert\".",
    },
    {
      path: "src/components/ui/primitives/Input.tsx",
      test: (src) =>
        /role="alert"/.test(src) &&
        /aria-invalid=\{isInvalid \|\| undefined\}/.test(src) &&
        /aria-describedby=\{describedBy\}/.test(src),
      message:
        "Shared Input errors must be announced and associated with the field.",
    },
    {
      path: "src/components/ui/primitives/Textarea.tsx",
      test: (src) =>
        /role="alert"/.test(src) &&
        /aria-invalid=\{isInvalid \|\| undefined\}/.test(src) &&
        /aria-describedby=\{describedBy\}/.test(src),
      message:
        "Shared Textarea errors must be announced and associated with the field.",
    },
    {
      path: "src/components/OfflineBanner.tsx",
      test: (src) => /aria-live="assertive"/.test(src) && /role="status"/.test(src),
      message: "OfflineBanner must remain a live announced connectivity status.",
    },
  ];

  for (const contract of contracts) {
    const full = join(root, contract.path);
    const src = existsSync(full) ? readFileSync(full, "utf8") : "";
    if (contract.test(src)) continue;
    addFinding(
      "a11y-contract",
      contract.path,
      1,
      contract.message,
      contract.path,
    );
  }
}

function groupedFindings(items) {
  const grouped = new Map();
  for (const item of items) {
    const current = grouped.get(item.key);
    if (current) {
      current.count += 1;
      current.lines.push(item.line);
    } else {
      grouped.set(item.key, { ...item, count: 1, lines: [item.line] });
    }
  }
  return [...grouped.values()].sort((a, b) =>
    a.rule.localeCompare(b.rule) ||
    a.path.localeCompare(b.path) ||
    a.message.localeCompare(b.message),
  );
}

function loadBaseline() {
  if (!existsSync(BASELINE_PATH)) return { findings: [] };
  return JSON.parse(readFileSync(BASELINE_PATH, "utf8"));
}

function writeBaseline(items) {
  const payload = {
    version: 1,
    generatedAt: new Date().toISOString(),
    note:
      "Known frontend UI-governance debt. The checker fails only when current count exceeds a baseline count for the same fingerprint.",
    findings: items.map(({ key, rule, path, message, snippet, count }) => ({
      key,
      rule,
      path,
      message,
      snippet,
      count,
    })),
  };
  writeFileSync(BASELINE_PATH, `${JSON.stringify(payload, null, 2)}\n`);
}

function compareToBaseline(current, baseline) {
  const baselineMap = new Map(
    (baseline.findings ?? []).map((item) => [item.key, item.count ?? 1]),
  );
  const newItems = [];
  const knownItems = [];
  for (const item of current) {
    const allowedCount = baselineMap.get(item.key) ?? 0;
    if (item.count > allowedCount) {
      newItems.push({ ...item, newCount: item.count - allowedCount });
    } else {
      knownItems.push(item);
    }
  }
  const currentKeys = new Set(current.map((item) => item.key));
  const reducedCount = (baseline.findings ?? []).filter(
    (item) => !currentKeys.has(item.key),
  ).length;
  return { newItems, knownItems, reducedCount };
}

walk(SRC_ROOT);

for (const full of files) {
  const src = readFileSync(full, "utf8");
  const path = relative(APP_ROOT, full).replaceAll("\\", "/");
  const lines = src.split(/\r?\n/);
  scanDarkUtilities(path, lines);
  scanMobileDialogs(path, src);
  scanErrorLiveRegions(path, lines);
}
scanSharedA11yContracts(APP_ROOT);

const current = groupedFindings(findings);

if (updateBaseline) {
  writeBaseline(current);
  console.log(`✓ Updated ${relative(process.cwd(), BASELINE_PATH)} with ${current.length} known findings.`);
  process.exit(0);
}

const baseline = loadBaseline();
const { newItems, knownItems, reducedCount } = compareToBaseline(current, baseline);
const changeScope = getGitChangeScope({ startDir: APP_ROOT });
const changedFiles = changeScope.files;
const appRepoPath = repoRelativePath(changeScope.repoRoot, APP_ROOT);
const touchedKnownItems = knownItems.filter(
  (item) => changedFiles.has(`${appRepoPath}/${item.path}`),
);

const byRule = new Map();
for (const item of current) byRule.set(item.rule, (byRule.get(item.rule) ?? 0) + item.count);
const ruleSummary = [...byRule.entries()]
  .sort(([a], [b]) => a.localeCompare(b))
  .map(([rule, count]) => `${rule}=${count}`)
  .join(", ");

if (newItems.length === 0 && touchedKnownItems.length === 0) {
  console.log(
    `✓ UI governance check passed: no new findings (${knownItems.length} known fingerprints; ${ruleSummary || "0 findings"}).`,
  );
  if (reducedCount > 0) {
    console.log(`  ${reducedCount} baseline fingerprint(s) no longer reproduce; run with --update-baseline after confirming.`);
  }
  if (verbose && knownItems.length > 0) {
    for (const item of knownItems) {
      console.log(`  known ${item.rule} ${item.path}:${item.lines[0]} ${item.message}`);
    }
  }
  process.exit(0);
}

console.error(
  `✗ UI governance check failed: ${newItems.length} new fingerprint(s), ${touchedKnownItems.length} touched debt fingerprint(s).`,
);
for (const item of newItems.slice(0, 30)) {
  console.error(
    `  ${item.rule} ${item.path}:${item.lines[0]} (+${item.newCount}) ${item.message}`,
  );
  console.error(`    ${item.snippet}`);
}
if (newItems.length > 30) {
  console.error(`  ...and ${newItems.length - 30} more.`);
}
for (const item of touchedKnownItems.slice(0, 30)) {
  console.error(
    `  touched debt ${item.rule} ${item.path}:${item.lines[0]} ${item.message}`,
  );
}
if (touchedKnownItems.length > 30) {
  console.error(`  ...and ${touchedKnownItems.length - 30} more touched debt findings.`);
}
console.error(
  "\nFix the UI, add a narrow @ui-governance-allow media/code/danger/scrim comment for true exceptions, or intentionally refresh the baseline.",
);
process.exit(1);
