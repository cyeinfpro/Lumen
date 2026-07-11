import assert from "node:assert/strict";
import test from "node:test";

const { createRequestFence } = await import(
  new URL("./requestGuards.ts", import.meta.url).href
);

test("request fence rejects a response after identity changes", async () => {
  const fence = createRequestFence();
  const startedAt = fence.snapshot();
  let resolveRequest!: (value: string) => void;
  const request = new Promise<string>((resolve) => {
    resolveRequest = resolve;
  });

  fence.advance();
  resolveRequest("old-user-response");
  await request;

  assert.equal(fence.isCurrent(startedAt), false);
});
