import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const testDir = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(testDir, "..");
const repositoryRoot = path.resolve(webRoot, "../..");

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
