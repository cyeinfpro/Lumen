import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { ESLint } from "eslint";

const MAX_COMPLEXITY = 15;
const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(scriptDir, "..");
const baselinePath = path.join(scriptDir, "complexity-baseline.json");
const updateBaseline = process.argv.includes("--update-baseline");

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
for (const [key, complexity] of Object.entries(current)) {
  const allowed = baseline.violations[key];
  if (allowed === undefined) {
    errors.push(`new complexity violation: ${key} (${complexity})`);
  } else if (complexity > allowed) {
    errors.push(`complexity grew: ${key} ${allowed} -> ${complexity}`);
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
