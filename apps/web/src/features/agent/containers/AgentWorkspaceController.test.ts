import assert from "node:assert/strict";
import test from "node:test";

import type { Generation } from "@/lib/types";
import {
  AgentRefreshCoordinator,
  mergeAgentGeneration,
  selectAgentGenerationChannelIds,
} from "./agentRealtime.ts";


function generation(
  id: string,
  status: Generation["status"],
  createdAt: number,
): Generation {
  return {
    id,
    message_id: "message-1",
    action: "generate",
    prompt: "prompt",
    size_requested: "1024x1024",
    aspect_ratio: "1:1",
    input_image_ids: [],
    primary_input_image_id: null,
    status,
    stage: "queued",
    attempt: 0,
    started_at: createdAt,
    created_at: createdAt,
  };
}

test("Agent generation channels prioritize newest nonterminal tasks", () => {
  const generations: Record<string, Generation> = {};
  const owners: Record<string, string> = {};
  for (let index = 0; index < 60; index += 1) {
    const id = `generation-${String(index).padStart(2, "0")}`;
    generations[id] = generation(
      id,
      index % 2 === 0 ? "running" : "queued",
      index,
    );
    owners[id] = "session-1";
  }

  assert.deepEqual(
    selectAgentGenerationChannelIds(generations, owners, "session-1"),
    Array.from(
      { length: 60 },
      (_value, index) => `generation-${String(59 - index).padStart(2, "0")}`,
    ),
  );
});

test("Agent refreshes run as one in-flight request plus one trailing pass", async () => {
  const coordinator = new AgentRefreshCoordinator();
  let release: (() => void) | undefined;
  const first = new Promise<void>((resolve) => {
    release = resolve;
  });
  let calls = 0;
  const refresh = async () => {
    calls += 1;
    if (calls === 1) await first;
  };

  coordinator.request(refresh);
  coordinator.request(refresh);
  coordinator.request(refresh);
  assert.equal(calls, 1);
  release?.();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(calls, 2);
});

test("stale running generation snapshots cannot regress terminal state", () => {
  const terminal = generation("generation-terminal", "succeeded", 2);
  const stale = generation("generation-terminal", "running", 1);

  assert.equal(mergeAgentGeneration(terminal, stale).status, "succeeded");
});
