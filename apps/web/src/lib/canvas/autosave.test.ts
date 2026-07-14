import assert from "node:assert/strict";
import test from "node:test";

const {
  CANVAS_AUTOSAVE_OPERATION_LIMIT,
  RetryableAutosaveBatchReader,
  SerialAutosave,
  takeAtomicAutosaveOperations,
  takeAutosaveOperations,
} = await import("#canvas-autosave");

function deferred() {
  let resolve: (() => void) | undefined;
  const promise = new Promise<void>((next) => {
    resolve = next;
  });
  return {
    promise,
    resolve: () => resolve?.(),
  };
}

async function waitFor(predicate: () => boolean): Promise<void> {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if (predicate()) return;
    await new Promise<void>((resolve) => setImmediate(resolve));
  }
  assert.ok(predicate(), "condition was not reached");
}

test("queued flush callers wait for the serial rerun to finish", async () => {
  const batches = ["one", "two"];
  const timeline: string[] = [];
  const firstGate = deferred();
  const secondGate = deferred();
  const autosave = new SerialAutosave<string>({
    delayMs: 1,
    readBatch: () => {
      const payload = batches.shift();
      return payload ? { count: 1, payload } : null;
    },
    sendBatch: async ({ payload }) => {
      timeline.push(`start:${payload}`);
      if (payload === "one") await firstGate.promise;
      if (payload === "two") await secondGate.promise;
      timeline.push(`end:${payload}`);
    },
  });

  const first = autosave.flush();
  await waitFor(() => timeline.includes("start:one"));
  const second = autosave.flush();
  assert.deepEqual(timeline, ["start:one"]);
  let secondSettled = false;
  void second.then(() => {
    secondSettled = true;
  });
  await Promise.resolve();
  assert.equal(secondSettled, false);

  firstGate.resolve();
  await waitFor(() => timeline.includes("start:two"));
  assert.equal(secondSettled, false);
  secondGate.resolve();
  await second;
  await first;
  assert.deepEqual(timeline, ["start:one", "end:one", "start:two", "end:two"]);
  autosave.stop();
});

test("failed autosave does not replay a stale rerun until explicitly retried", async () => {
  const batches = ["one", "two"];
  const attempts: string[] = [];
  const firstGate = deferred();
  const autosave = new SerialAutosave<string>({
    readBatch: () => {
      const payload = batches.shift();
      return payload ? { count: 1, payload } : null;
    },
    sendBatch: async ({ payload }) => {
      attempts.push(payload);
      if (payload === "one") {
        await firstGate.promise;
        throw new Error("save failed");
      }
    },
  });

  const first = autosave.flush();
  await waitFor(() => attempts.includes("one"));
  const queued = autosave.flush();
  let queuedSettled = false;
  void queued.then(() => {
    queuedSettled = true;
  });
  await Promise.resolve();
  assert.equal(queuedSettled, false);

  firstGate.resolve();
  await Promise.all([first, queued]);
  assert.deepEqual(attempts, ["one"]);

  await autosave.flush();
  assert.deepEqual(attempts, ["one", "two"]);
  autosave.stop();
});

test("retryable batch reader preserves the exact failed batch until acknowledged", () => {
  const batches = [
    { count: 1, payload: { mutationId: "first", value: "sent" } },
    { count: 1, payload: { mutationId: "second", value: "newer" } },
  ];
  const reader = new RetryableAutosaveBatchReader(() => batches.shift() ?? null);

  const first = reader.read();
  assert.equal(first?.payload.mutationId, "first");
  assert.equal(reader.read(), first);
  assert.equal(batches.length, 1);

  assert.ok(first);
  reader.acknowledge(first);
  assert.equal(reader.read()?.payload.mutationId, "second");
});

test("failed serial flush resets active state and retries the protected batch first", async () => {
  const batches = [
    { count: 1, payload: "protected" },
    { count: 1, payload: "tail" },
  ];
  const reader = new RetryableAutosaveBatchReader(
    () => batches.shift() ?? null,
  );
  const attempts: string[] = [];
  let shouldFail = true;
  const autosave = new SerialAutosave<string>({
    readBatch: () => reader.read(),
    sendBatch: async (batch) => {
      attempts.push(batch.payload);
      if (shouldFail) {
        shouldFail = false;
        throw new Error("network");
      }
      reader.acknowledge(batch);
    },
  });

  await autosave.flush();
  assert.deepEqual(attempts, ["protected"]);

  await autosave.flush();
  assert.deepEqual(attempts, ["protected", "protected"]);

  await autosave.flush();
  assert.deepEqual(attempts, ["protected", "protected", "tail"]);
  autosave.stop();
});

test("autosave operation batches stay within the API contract", () => {
  const operations = Array.from(
    { length: CANVAS_AUTOSAVE_OPERATION_LIMIT + 27 },
    (_, index) => index,
  );

  assert.equal(
    takeAutosaveOperations(operations).length,
    CANVAS_AUTOSAVE_OPERATION_LIMIT,
  );
  assert.deepEqual(takeAutosaveOperations(operations, 2), [0, 1]);
});

test("autosave never splits an atomic operation group", () => {
  const operations = Array.from(
    { length: CANVAS_AUTOSAVE_OPERATION_LIMIT + 1 },
    (_, index) => index,
  );

  assert.equal(
    takeAtomicAutosaveOperations(
      operations,
      [CANVAS_AUTOSAVE_OPERATION_LIMIT - 1, 2],
    ).length,
    CANVAS_AUTOSAVE_OPERATION_LIMIT - 1,
  );
  assert.deepEqual(
    takeAtomicAutosaveOperations(operations, [operations.length]),
    [],
  );
});
