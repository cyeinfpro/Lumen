import { strictEqual, match } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import type { UpdateStepRecord } from "@/lib/apiClient";

const { updateOutcome } = await import(new URL("./AdminUpdatePanel.outcome.ts", import.meta.url).href) as typeof import("./AdminUpdatePanel.outcome");

function phase(name: string, status: UpdateStepRecord["status"] = "done", rc: number | null = 0): UpdateStepRecord {
  return { phase: name, status, rc, info: {}, started_at: "2026-09-13T00:00:00Z", ended_at: null, dur_ms: null };
}

test("idle, info-only checks, warm pulls and cleanup are not successful deployments", () => {
  strictEqual(updateOutcome(false, []), "idle");
  strictEqual(updateOutcome(false, [phase("check", "running", null)]), "incomplete");
  strictEqual(updateOutcome(false, [phase("warm_pull")]), "incomplete");
  strictEqual(updateOutcome(false, [phase("health_check"), phase("cleanup")]), "incomplete");
  strictEqual(updateOutcome(false, [phase("complete", "done", null)]), "incomplete");
});

test("only an explicit successful terminal record confirms completion", () => {
  strictEqual(updateOutcome(false, [phase("complete")]), "complete");
  strictEqual(updateOutcome(false, [phase("rollback")]), "complete");
  strictEqual(updateOutcome(false, [phase("check", "done", 1)]), "failed");
  strictEqual(updateOutcome(false, [phase("complete")], true), "failed");
  strictEqual(updateOutcome(true, [phase("check", "done", 1)], true), "running");
  strictEqual(updateOutcome(false, [], true), "failed");
});

test("automatic reload and the success progress bar require confirmed completion", () => {
  const consoleSource = readFileSync(new URL("./AdminUpdatePanel.console.tsx", import.meta.url), "utf8");
  match(consoleSource, /if \(!wasRunning \|\| running \|\| !complete\) return/);
  match(consoleSource, /const progressPct = complete \? 100/);
  const statusSource = readFileSync(new URL("./AdminUpdatePanel.status.tsx", import.meta.url), "utf8");
  match(statusSource, /return complete \? "更新已完成" : "更新尚未完成"/);
});
