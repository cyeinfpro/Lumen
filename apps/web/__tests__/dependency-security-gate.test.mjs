import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const testDir = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(testDir, "..");
const repositoryRoot = path.resolve(webRoot, "../..");

function readJson(relativePath) {
  return JSON.parse(readFileSync(path.join(webRoot, relativePath), "utf8"));
}

test("production dependency policy pins the audited safe ranges", () => {
  const packageJson = readJson("package.json");

  assert.equal(packageJson.dependencies["@sentry/nextjs"], "~10.54.0");
  assert.equal(packageJson.dependencies.next, "~16.2.11");
  assert.equal(packageJson.devDependencies["eslint-config-next"], "~16.2.11");
  assert.deepEqual(packageJson.overrides, {
    "@babel/core": "7.29.7",
    "fast-uri": "3.1.5",
    postcss: "8.5.25",
    sharp: "0.35.3",
  });
  assert.equal(
    Object.hasOwn(packageJson.overrides, "brace-expansion"),
    false,
  );
});

test("lockfile resolves the audited production dependency set", () => {
  const packageJson = readJson("package.json");
  const lock = readJson("package-lock.json");
  const root = lock.packages[""];

  assert.equal(
    root.dependencies["@sentry/nextjs"],
    packageJson.dependencies["@sentry/nextjs"],
  );
  assert.equal(root.dependencies.next, packageJson.dependencies.next);
  assert.equal(
    root.devDependencies["eslint-config-next"],
    packageJson.devDependencies["eslint-config-next"],
  );

  const expectedVersions = {
    "node_modules/@babel/core": "7.29.7",
    "node_modules/@sentry/nextjs": "10.54.0",
    "node_modules/eslint-config-next": "16.2.12",
    "node_modules/fast-uri": "3.1.5",
    "node_modules/next": "16.2.12",
    "node_modules/postcss": "8.5.25",
    "node_modules/sharp": "0.35.3",
  };
  for (const [packagePath, expectedVersion] of Object.entries(
    expectedVersions,
  )) {
    assert.equal(
      lock.packages[packagePath]?.version,
      expectedVersion,
      `${packagePath} must resolve to ${expectedVersion}`,
    );
  }
});

test("frontend CI rejects vulnerable production dependencies before tests", () => {
  const workflow = readFileSync(
    path.join(repositoryRoot, ".github/workflows/ci.yml"),
    "utf8",
  );
  const frontendStart = workflow.indexOf("\n  frontend:");
  assert.notEqual(frontendStart, -1, "frontend job must exist");
  const frontend = workflow.slice(frontendStart);

  const installIndex = frontend.indexOf("      - run: npm ci");
  const auditName =
    "      - name: Reject vulnerable production dependencies";
  const auditIndex = frontend.indexOf(auditName);
  const firstTestIndex = frontend.indexOf(
    "      - name: Run impacted frontend tests",
  );

  assert.notEqual(installIndex, -1, "frontend job must install with npm ci");
  assert.notEqual(auditIndex, -1, "frontend job must include production audit");
  assert.notEqual(firstTestIndex, -1, "frontend test steps must exist");
  assert.ok(installIndex < auditIndex, "production audit must follow npm ci");
  assert.ok(auditIndex < firstTestIndex, "production audit must precede tests");

  const nextStepIndex = frontend.indexOf("\n      - ", auditIndex + 1);
  const auditStep = frontend.slice(
    auditIndex,
    nextStepIndex === -1 ? undefined : nextStepIndex,
  );
  assert.match(
    auditStep,
    /\n        run: npm audit --omit=dev --audit-level=high(?:\n|$)/,
  );
  assert.doesNotMatch(auditStep, /\n        (?:if|continue-on-error):/);
});
