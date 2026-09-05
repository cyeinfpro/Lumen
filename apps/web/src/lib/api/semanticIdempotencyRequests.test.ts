import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import "../../store/chat/moduleResolution.test-helper.mjs";

const { semanticRequestFingerprint, SemanticIdempotencyStore } = await import("./semanticIdempotency.ts");
const digest = (value: string) => createHash("sha256").update(value).digest("hex");

class MemoryStorage {
  values = new Map<string, string>();
  get length() { return this.values.size; }
  key(index: number) { return [...this.values.keys()][index] ?? null; }
  getItem(key: string) { return this.values.get(key) ?? null; }
  setItem(key: string, value: string) { this.values.set(key, value); }
  removeItem(key: string) { this.values.delete(key); }
}

test("JSON canonicalization preserves own __proto__ recursively without aliasing legacy collisions", () => {
  const first = JSON.parse('{"__proto__":{"x":1},"text":"same"}');
  const second = JSON.parse('{"text":"same","__proto__":{"x":2}}');
  assert.equal(semanticRequestFingerprint({}, first), '{"payload":{"__proto__":{"x":1},"text":"same"},"scope":{}}');
  assert.notEqual(semanticRequestFingerprint({}, first), semanticRequestFingerprint({}, second));
  assert.notEqual(semanticRequestFingerprint({}, first), semanticRequestFingerprint({}, { text: "same" }));
  assert.notEqual(semanticRequestFingerprint(first, {}), semanticRequestFingerprint(second, {}));
  assert.notEqual(semanticRequestFingerprint({}, [first]), semanticRequestFingerprint({}, [second]));
  assert.notEqual(semanticRequestFingerprint({}, [1, 2]), semanticRequestFingerprint({}, [2, 1]));
  assert.equal(new Set([null, [], {}].map((value) => semanticRequestFingerprint({}, value))).size, 3);
  assert.equal(Object.getPrototypeOf(first), Object.prototype);
  assert.equal(Object.hasOwn(Object.prototype, "x"), false);
});

test("ordinary legacy pending fingerprints replay byte-for-byte after reload and snapshot confirmation advances intent", async () => {
  const scope = { sessionId: "session", operation: "agent.message.create", userId: "owner" };
  const payload = { text: "same", nested: { z: [1, null, {}], a: true } };
  const legacy = '{"payload":{"nested":{"a":true,"z":[1,null,{}]},"text":"same"},"scope":{"operation":"agent.message.create","sessionId":"session","userId":"owner"}}';
  assert.equal(semanticRequestFingerprint(scope, payload), legacy);
  const storage = new MemoryStorage();
  const options = { storage, storageKey: "requests-regression", digest, lockRequest: null };
  const original = new SemanticIdempotencyStore({
    ...options,
    digest: (value) => {
      if (value.startsWith("request:")) assert.equal(value, `request:${legacy}`);
      return digest(value);
    },
  });
  await original.activateIdentity("owner");
  const lease = await original.acquire(scope, payload);
  await original.markSubmitted(lease);
  await original.recordFailure(lease, { status: 504 });
  const reload = new SemanticIdempotencyStore(options);
  await reload.activateIdentity("owner");
  const replay = await reload.acquire(scope, { nested: { a: true, z: [1, null, {}] }, text: "same" });
  assert.equal(replay.key, lease.key);
  await reload.confirmPendingKey("other-owner", lease.key);
  await reload.confirmPendingKey("owner", "unknown-key");
  assert.equal((await reload.acquire(scope, payload)).key, lease.key);
  await reload.confirmPendingKey("owner", lease.key);
  const repeated = await reload.acquire(scope, payload);
  assert.notEqual(repeated.key, lease.key);
  assert.equal(repeated.generation, lease.generation + 1);
  await reload.confirmPendingKey("owner", lease.key);
  assert.equal((await reload.acquire(scope, payload)).key, repeated.key);
});

test("snapshot key confirmation cannot cross an identity epoch while waiting for a lock", async () => {
  let release: (() => void) | undefined;
  let hold = false;
  const store = new SemanticIdempotencyStore({
    storage: new MemoryStorage(), digest,
    lockRequest: async (_name, _options, callback) => {
      if (hold) await new Promise<void>((resolve) => { release = resolve; });
      return callback();
    },
  });
  await store.activateIdentity("owner-a");
  const first = await store.acquire({ operation: "agent.message.create" }, {});
  hold = true;
  const pending = store.confirmPendingKey("owner-a", first.key);
  hold = false;
  await store.activateIdentity("owner-b");
  const second = await store.acquire({ operation: "agent.message.create" }, {});
  release?.();
  await pending;
  assert.equal((await store.acquire({ operation: "agent.message.create" }, {})).key, second.key);
  await store.activateIdentity("owner-a");
  assert.equal((await store.acquire({ operation: "agent.message.create" }, {})).key, first.key);
});
