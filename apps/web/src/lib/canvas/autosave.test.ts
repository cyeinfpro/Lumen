import assert from "node:assert/strict";
import test from "node:test";

const { SerialAutosave } = await import("#canvas-autosave");

test("autosave batches remain strictly serial", async () => {
  const batches = ["one", "two"];
  const timeline: string[] = [];
  let releaseFirst: (() => void) | undefined;
  const firstGate = new Promise<void>((resolve) => {
    releaseFirst = resolve;
  });
  const autosave = new SerialAutosave<string>({
    delayMs: 1,
    readBatch: () => {
      const payload = batches.shift();
      return payload ? { count: 1, payload } : null;
    },
    sendBatch: async ({ payload }) => {
      timeline.push(`start:${payload}`);
      if (payload === "one") await firstGate;
      timeline.push(`end:${payload}`);
    },
  });

  const first = autosave.flush();
  const second = autosave.flush();
  await Promise.resolve();
  assert.deepEqual(timeline, ["start:one"]);
  releaseFirst?.();
  await Promise.all([first, second]);
  assert.deepEqual(timeline, ["start:one", "end:one", "start:two", "end:two"]);
  autosave.stop();
});

test("failed autosave does not replay a stale rerun until explicitly retried", async () => {
  const batches = ["one", "two"];
  const attempts: string[] = [];
  let releaseFirst: (() => void) | undefined;
  const firstGate = new Promise<void>((resolve) => {
    releaseFirst = resolve;
  });
  const autosave = new SerialAutosave<string>({
    readBatch: () => {
      const payload = batches.shift();
      return payload ? { count: 1, payload } : null;
    },
    sendBatch: async ({ payload }) => {
      attempts.push(payload);
      if (payload === "one") {
        await firstGate;
        throw new Error("save failed");
      }
    },
  });

  const first = autosave.flush();
  const queued = autosave.flush();
  releaseFirst?.();
  await Promise.all([first, queued]);
  assert.deepEqual(attempts, ["one"]);

  await autosave.flush();
  assert.deepEqual(attempts, ["one", "two"]);
  autosave.stop();
});
