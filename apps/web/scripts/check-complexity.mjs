import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";

import { ESLint } from "eslint";

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
  const labelMatch = message.message.match(
    /^(?:Async )?(?:Function|Method) '([^']+)'/,
  );
  return labelMatch?.[1] ?? "anonymous";
}

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
for (const result of results) {
  const occurrences = new Map();
  for (const message of result.messages) {
    if (message.ruleId !== "complexity") continue;
    const complexity = findingComplexity(message);
    if (complexity === null) continue;
    const label = findingLabel(message);
    const occurrence = (occurrences.get(label) ?? 0) + 1;
    occurrences.set(label, occurrence);
    current[findingKey(result, label, occurrence)] = complexity;
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
  const previousAllowed = previousBaseline.violations?.[key];
  const sourcePath = key.split("::", 1)[0];
  const touched =
    changedFiles.has(`apps/web/${sourcePath}`) || changedFiles.has(sourcePath);
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
