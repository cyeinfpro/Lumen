import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";

import { ESLint } from "eslint";
import ts from "typescript";

const MAX_COMPLEXITY = 15;
const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(scriptDir, "..");
const baselinePath = path.join(scriptDir, "complexity-baseline.json");
const updateBaseline = process.argv.includes("--update-baseline");

function gitChangedFiles() {
  const run = (args) => {
    try {
      return execFileSync("git", args, {
        cwd: root,
        encoding: "utf8",
        stdio: ["ignore", "pipe", "ignore"],
      })
        .split(/\r?\n/)
        .filter(Boolean)
        .map((value) => value.replaceAll("\\", "/"));
    } catch {
      return [];
    }
  };

  const working = new Set([
    ...run(["diff", "--name-only", "--diff-filter=ACMR"]),
    ...run(["diff", "--cached", "--name-only", "--diff-filter=ACMR"]),
  ]);
  if (working.size > 0) return { files: working, baseRef: "HEAD" };
  return {
    files: new Set(
      run(["diff", "--name-only", "--diff-filter=ACMR", "HEAD^", "HEAD"]),
    ),
    baseRef: "HEAD^",
  };
}

function loadPreviousBaseline(baseRef, fallback) {
  try {
    const raw = execFileSync(
      "git",
      ["show", `${baseRef}:apps/web/scripts/complexity-baseline.json`],
      {
        cwd: root,
        encoding: "utf8",
        stdio: ["ignore", "pipe", "ignore"],
      },
    );
    return JSON.parse(raw);
  } catch {
    return fallback;
  }
}

function findingLabel(message) {
  const labelMatch =
    message.message.match(/^(?:Async )?(?:Function|Method) '([^']+)'/) ??
    message.message.match(/^(?:Async )?method '([^']+)'/);
  return labelMatch?.[1] ?? "anonymous";
}

const previousBaselineAliases = {
  "src/app/video/video-task-ui.tsx::TaskRow":
    "src/app/video/page.tsx::TaskRow",
  "src/store/useChatStore.ts::loadHistoricalMessages":
    "src/store/useChatStore.ts::anonymous#5",
  "src/store/useChatStore.ts::sendMessage":
    "src/store/useChatStore.ts::anonymous#6",
};

function findingKey(result, label, occurrence) {
  const relative = path.relative(root, result.filePath).split(path.sep).join("/");
  const suffix = occurrence > 1 ? `#${occurrence}` : "";
  return `${relative}::${label}${suffix}`;
}

function findingComplexity(message) {
  const match = message.message.match(
    /complexity of (\d+)\. Maximum allowed is \d+\./,
  );
  return match ? Number.parseInt(match[1], 10) : null;
}

function isFunctionLikeNode(node) {
  return (
    ts.isFunctionDeclaration(node) ||
    ts.isFunctionExpression(node) ||
    ts.isArrowFunction(node) ||
    ts.isMethodDeclaration(node) ||
    ts.isGetAccessorDeclaration(node) ||
    ts.isSetAccessorDeclaration(node) ||
    ts.isConstructorDeclaration(node)
  );
}

const functionRangeCache = new Map();

function findingFunctionRange(filePath, line, column) {
  const cacheKey = `${filePath}:${line}:${column}`;
  const cached = functionRangeCache.get(cacheKey);
  if (cached) return cached;

  const sourceText = fs.readFileSync(filePath, "utf8");
  const sourceFile = ts.createSourceFile(
    filePath,
    sourceText,
    ts.ScriptTarget.Latest,
    true,
    filePath.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const position = sourceFile.getPositionOfLineAndCharacter(
    Math.max(0, line - 1),
    Math.max(0, column - 1),
  );
  let match = null;
  let sameLineMatch = null;
  const visit = (node) => {
    if (isFunctionLikeNode(node)) {
      const startLine =
        sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
      if (
        startLine === line &&
        (sameLineMatch === null ||
          node.end - node.getStart(sourceFile) <
            sameLineMatch.end - sameLineMatch.getStart(sourceFile))
      ) {
        sameLineMatch = node;
      }
    }
    if (position < node.getFullStart() || position > node.end) return;
    if (isFunctionLikeNode(node)) match = node;
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);

  const selected = sameLineMatch ?? match;
  const range = selected
    ? {
        start:
          sourceFile.getLineAndCharacterOfPosition(selected.getStart(sourceFile))
            .line + 1,
        end: sourceFile.getLineAndCharacterOfPosition(selected.end).line + 1,
      }
    : { start: line, end: line };
  functionRangeCache.set(cacheKey, range);
  return range;
}

const changedLineCache = new Map();

function changedLinesForFile(baseRef, sourcePath) {
  const cacheKey = `${baseRef}:${sourcePath}`;
  const cached = changedLineCache.get(cacheKey);
  if (cached) return cached;

  let diff = "";
  try {
    diff = execFileSync(
      "git",
      ["diff", "--unified=0", "--no-color", baseRef, "--", sourcePath],
      {
        cwd: root,
        encoding: "utf8",
        stdio: ["ignore", "pipe", "ignore"],
      },
    );
  } catch {
    changedLineCache.set(cacheKey, null);
    return null;
  }

  const lines = new Set();
  for (const rawLine of diff.split(/\r?\n/)) {
    const match = rawLine.match(/^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@/);
    if (!match) continue;
    const start = Number.parseInt(match[1], 10);
    const count = match[2] === undefined ? 1 : Number.parseInt(match[2], 10);
    if (count === 0) {
      lines.add(Math.max(1, start));
      lines.add(Math.max(1, start + 1));
      continue;
    }
    for (let line = start; line < start + count; line += 1) lines.add(line);
  }
  changedLineCache.set(cacheKey, lines);
  return lines;
}

const eslint = new ESLint({
  cwd: root,
  overrideConfig: {
    rules: {
      complexity: ["error", MAX_COMPLEXITY],
    },
  },
});
const results = await eslint.lintFiles(["src/**/*.{ts,tsx}"]);
const current = {};
const findingRanges = {};
for (const result of results) {
  const occurrences = new Map();
  for (const message of result.messages) {
    if (message.ruleId !== "complexity") continue;
    const complexity = findingComplexity(message);
    if (complexity === null) continue;
    const label = findingLabel(message);
    const occurrence = (occurrences.get(label) ?? 0) + 1;
    occurrences.set(label, occurrence);
    const key = findingKey(result, label, occurrence);
    current[key] = complexity;
    findingRanges[key] = findingFunctionRange(
      result.filePath,
      message.line,
      message.column,
    );
  }
}

if (updateBaseline) {
  fs.writeFileSync(
    baselinePath,
    `${JSON.stringify(
      {
        version: 2,
        max_complexity: MAX_COMPLEXITY,
        violations: Object.fromEntries(
          Object.entries(current).sort(([left], [right]) =>
            left.localeCompare(right),
          ),
        ),
      },
      null,
      2,
    )}\n`,
  );
  console.log(
    `Updated scripts/complexity-baseline.json with ${Object.keys(current).length} entries.`,
  );
  process.exit(0);
}

const baseline = JSON.parse(fs.readFileSync(baselinePath, "utf8"));
if (
  baseline.version !== 2 ||
  baseline.max_complexity !== MAX_COMPLEXITY ||
  typeof baseline.violations !== "object" ||
  baseline.violations === null
) {
  throw new Error("Unsupported frontend complexity baseline");
}

const errors = [];
const changed = gitChangedFiles();
const changedFiles = changed.files;
const previousBaseline = loadPreviousBaseline(changed.baseRef, baseline);
for (const [key, complexity] of Object.entries(current)) {
  const allowed = baseline.violations[key];
  const previousKey = previousBaselineAliases[key] ?? key;
  const previousAllowed = previousBaseline.violations?.[previousKey];
  const sourcePath = key.split("::", 1)[0];
  const fileTouched =
    changedFiles.has(`apps/web/${sourcePath}`) || changedFiles.has(sourcePath);
  const changedLines = fileTouched
    ? changedLinesForFile(changed.baseRef, sourcePath)
    : new Set();
  const range = findingRanges[key];
  const touched =
    fileTouched &&
    (changedLines === null ||
      (range !== undefined &&
        [...changedLines].some(
          (line) => line >= range.start && line <= range.end,
        )));
  if (allowed === undefined) {
    errors.push(`new complexity violation: ${key} (${complexity})`);
  } else if (complexity > allowed) {
    errors.push(`complexity grew: ${key} ${allowed} -> ${complexity}`);
  }
  if (
    touched &&
    previousAllowed !== undefined &&
    complexity >= previousAllowed
  ) {
    errors.push(
      `touched complexity debt must decrease: ${key} ${previousAllowed} -> ${complexity}`,
    );
  } else if (
    touched &&
    previousAllowed === undefined &&
    complexity > MAX_COMPLEXITY
  ) {
    errors.push(`new touched complexity debt: ${key} (${complexity})`);
  }
}

if (errors.length > 0) {
  console.error("Frontend complexity budget failed:");
  for (const error of errors) console.error(`- ${error}`);
  process.exit(1);
}

const removed = Object.keys(baseline.violations).filter(
  (key) => current[key] === undefined,
).length;
console.log(
  `Frontend complexity budget passed: ${Object.keys(current).length} grandfathered violations, ${removed} removed.`,
);
