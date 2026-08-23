import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("./AgentWorkspaceController.tsx", import.meta.url),
  "utf8",
);

test("Agent snapshot polling is not restarted by each active run snapshot", () => {
  assert.match(
    source,
    /const snapshotPollIntervalMs = activeRun \? 2_000 : 8_000;/,
  );
  assert.match(source, /window\.setTimeout\(run, snapshotPollIntervalMs\);/);
  assert.match(
    source,
    /\[currentSessionId, refreshSnapshot, setRealtimeStatus, snapshotPollIntervalMs\]/,
  );
  assert.doesNotMatch(
    source,
    /\[activeRun, currentSessionId, refreshSnapshot, setRealtimeStatus\]/,
  );
});
