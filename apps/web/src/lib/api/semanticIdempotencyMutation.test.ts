import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import "../../store/chat/moduleResolution.test-helper.mjs";
import type {
  DurableStorage,
  ExclusiveLockGuard,
  ExclusiveLockRequest,
} from "./semanticIdempotencyPersistence";

const { SemanticIdempotencyDurabilityError, SemanticIdempotencyStore } =
  await import("./semanticIdempotency.ts");
const { writeStorageRaw } = await import("./semanticIdempotencyStorage.ts");

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

class GuardedStorage implements DurableStorage {
  readonly values = new Map<string, string>();
  mutationDepth = 0;
  rootWrites = 0;
  entryWrites = 0;
  entryRemovals = 0;

  get length(): number {
    return this.values.size;
  }

  key(index: number): string | null {
    return [...this.values.keys()][index] ?? null;
  }

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    assert.ok(this.mutationDepth > 0, `unguarded setItem: ${key}`);
    this.values.set(key, value);
    if (key.includes(".entry.")) this.entryWrites += 1;
    else if (!key.includes(".lock.")) this.rootWrites += 1;
  }

  removeItem(key: string): void {
    assert.ok(this.mutationDepth > 0, `unguarded removeItem: ${key}`);
    this.values.delete(key);
    if (key.includes(".entry.")) this.entryRemovals += 1;
  }
}

function guardedLock(storage: GuardedStorage): ExclusiveLockRequest {
  const guard: ExclusiveLockGuard = {
    assertCurrent() {},
    runMutation(mutation) {
      storage.mutationDepth += 1;
      try {
        return mutation();
      } finally {
        storage.mutationDepth -= 1;
      }
    },
  };
  return async (_name, _options, callback) => callback(guard);
}

test("all semantic root, resolved, compact, discard, and cleanup writes are guarded", async () => {
  const storage = new GuardedStorage();
  const store = new SemanticIdempotencyStore({
    storage,
    storageKey: "semantic-guarded-paths",
    digest,
    maxEntries: 1,
    lockRequest: guardedLock(storage),
  });
  await store.activateIdentity("user-a");

  const confirmed = await store.acquire("scope", { id: "confirmed" });
  await store.markSubmitted(confirmed);
  await store.confirm(confirmed);
  const discarded = await store.acquire("scope", { id: "discarded" });
  await store.discard(discarded);
  const cleanup = await store.acquire("scope", { id: "cleanup" });
  await store.markSubmitted(cleanup);
  await store.confirm(cleanup);
  await store.activateIdentity("user-b");

  assert.ok(storage.rootWrites >= 9);
  assert.equal(storage.entryWrites, 2);
  assert.equal(storage.entryRemovals, 1);
  assert.equal(storage.mutationDepth, 0);
});

test("five post-write fence failures restore the prior root", async () => {
  for (let probe = 1; probe <= 5; probe += 1) {
    const storage = new GuardedStorage();
    const key = `semantic-rollback-${probe}`;
    storage.mutationDepth = 1;
    storage.setItem(key, `prior-${probe}`);
    storage.mutationDepth = 0;
    const guard: ExclusiveLockGuard = {
      assertCurrent() {},
      runMutation(mutation, rollback) {
        storage.mutationDepth += 1;
        try {
          mutation();
          rollback?.();
          throw new SemanticIdempotencyDurabilityError(
            "storage lock ownership was lost",
          );
        } finally {
          storage.mutationDepth -= 1;
        }
      },
    };

    await assert.rejects(
      writeStorageRaw(storage, key, `stale-${probe}`, guard),
      /storage lock ownership was lost/,
    );
    assert.equal(storage.getItem(key), `prior-${probe}`);
  }
});
