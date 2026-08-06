import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import "../../store/chat/moduleResolution.test-helper.mjs";
import type { ExclusiveLockRequest } from "./semanticIdempotencyPersistence";

const {
  idempotentPostRequest,
  markDefinitiveRequestFailure,
  semanticJsonPostRequest,
  semanticPostRequest,
  semanticRequestFingerprint,
  SemanticIdempotencyDurabilityError,
  SemanticIdempotencyStore,
} = await import("./semanticIdempotency.ts");

function keySequence(): () => string {
  let sequence = 0;
  return () => `key-${++sequence}`;
}

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function serializedLockRequest(): ExclusiveLockRequest {
  let tail: Promise<void> = Promise.resolve();
  return async (_name, _options, callback) => {
    const prior = tail;
    let release!: () => void;
    tail = new Promise<void>((resolve) => {
      release = resolve;
    });
    await prior;
    try {
      return await callback();
    } finally {
      release();
    }
  };
}

class MemoryStorage {
  private readonly values = new Map<string, string>();
  private readonly maxItems: number;
  private writeMode: "ok" | "drop" | "throw" = "ok";
  private writeInterceptor:
    | ((key: string, value: string) => void)
    | null = null;

  constructor(maxItems = Number.POSITIVE_INFINITY) {
    this.maxItems = maxItems;
  }

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
    this.writeInterceptor?.(key, value);
    if (this.writeMode === "throw") throw new Error("storage write failed");
    if (this.writeMode === "drop") return;
    if (!this.values.has(key) && this.values.size >= this.maxItems) {
      const error = new Error("storage quota exceeded");
      error.name = "QuotaExceededError";
      throw error;
    }
    this.values.set(key, value);
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }

  setWrites(mode: "ok" | "drop" | "throw"): void {
    this.writeMode = mode;
  }

  interceptWrites(
    interceptor: ((key: string, value: string) => void) | null,
  ): void {
    this.writeInterceptor = interceptor;
  }

  clear(): void {
    this.values.clear();
  }

  keysMatching(pattern: string): string[] {
    return [...this.values.keys()].filter((key) => key.includes(pattern));
  }

  serialized(): string {
    return [...this.values.values()].join("\n");
  }
}

function persistedRoot(
  storage: MemoryStorage,
  storageKey: string,
  userId = "user-a",
): {
  version: number;
  namespace: string;
  sequence: number;
  pending: Record<
    string,
    { key: string; generation: number; expiresAt: number }
  >;
} {
  const base = JSON.parse(String(storage.getItem(storageKey))) as {
    version: number;
    namespace?: string;
  };
  if (base.version !== 4 || !base.namespace) return base as never;
  const identity = digest(`identity:${userId}`);
  return JSON.parse(
    String(
      storage.getItem(
        `${storageKey}.root.${base.namespace}.${identity}`,
      ),
    ),
  );
}

test("semantic fingerprints ignore object key order but isolate scope and payload", () => {
  const first = semanticRequestFingerprint(
    { operation: "send", conversationId: "conv-1" },
    { text: "hello", params: { fast: false, count: 1 } },
  );
  const reordered = semanticRequestFingerprint(
    { conversationId: "conv-1", operation: "send" },
    { params: { count: 1, fast: false }, text: "hello" },
  );
  const changedScope = semanticRequestFingerprint(
    { operation: "reroll", conversationId: "conv-1" },
    { text: "hello", params: { fast: false, count: 1 } },
  );
  const changedPayload = semanticRequestFingerprint(
    { operation: "send", conversationId: "conv-1" },
    { text: "changed", params: { fast: false, count: 1 } },
  );

  assert.equal(first, reordered);
  assert.notEqual(first, changedScope);
  assert.notEqual(first, changedPayload);
});

test("unknown failures retain a key while terminal success and failure advance it", async () => {
  const store = new SemanticIdempotencyStore({
    freshKey: keySequence(),
    digest,
  });
  const scope = { operation: "send", conversationId: "conv-1" };
  const payload = { text: "hello" };

  const first = await store.acquire(scope, payload);
  await store.recordFailure(first, new TypeError("response lost"));
  assert.equal((await store.acquire(scope, payload)).key, first.key);
  await store.recordFailure(first, { status: 503 });
  assert.equal((await store.acquire(scope, payload)).key, first.key);
  await store.recordFailure(first, { status: 425 });
  assert.equal((await store.acquire(scope, payload)).key, first.key);

  await store.recordFailure(
    first,
    markDefinitiveRequestFailure({ status: 502 }),
  );
  const afterDefinitiveFailure = await store.acquire(scope, payload);
  assert.notEqual(afterDefinitiveFailure.key, first.key);

  await store.recordFailure(afterDefinitiveFailure, { status: 502 });
  assert.equal(
    (await store.acquire(scope, payload)).key,
    afterDefinitiveFailure.key,
  );

  await store.confirm(afterDefinitiveFailure);
  const afterSuccess = await store.acquire(scope, payload);
  assert.notEqual(afterSuccess.key, afterDefinitiveFailure.key);

  await store.recordFailure(afterSuccess, {
    status: 409,
    code: "idempotency_replay_unavailable",
  });
  assert.equal((await store.acquire(scope, payload)).key, afterSuccess.key);

  await store.recordFailure(
    afterSuccess,
    markDefinitiveRequestFailure({
      status: 502,
      code: "idempotency_terminal_persist_unknown",
    }),
  );
  assert.equal((await store.acquire(scope, payload)).key, afterSuccess.key);

  await store.recordFailure(
    afterSuccess,
    markDefinitiveRequestFailure({ status: 502, code: "internal" }),
  );
  const afterInternal = await store.acquire(scope, payload);
  assert.notEqual(afterInternal.key, afterSuccess.key);

  await store.recordFailure(afterInternal, {
    status: 409,
    code: "idempotency_replay_unavailable",
  });
  assert.equal((await store.acquire(scope, payload)).key, afterInternal.key);

  await store.recordFailure(afterSuccess, {
    status: 409,
    code: "conflict",
  });
  assert.equal((await store.acquire(scope, payload)).key, afterInternal.key);

  await store.recordFailure(afterInternal, {
    status: 409,
    code: "conflict",
  });
  assert.notEqual((await store.acquire(scope, payload)).key, afterInternal.key);
});

test("confirmed intents never reuse a key after TTL or a fresh store", async () => {
  let now = 1_000;
  const storage = new MemoryStorage();
  const options = {
    ttlMs: 100,
    maxEntries: 1,
    now: () => now,
    storage,
    storageKey: "semantic-monotonic-intents",
    digest,
    lockRequest: null,
  };
  const scope = { operation: "poster_style.generate" };
  const payload = { prompt: "repeat confirmed intent", count: 1 };

  const firstStore = new SemanticIdempotencyStore(options);
  await firstStore.activateIdentity("user-a");
  const first = await firstStore.acquire(scope, payload);
  await firstStore.confirm(first);

  now = 1_200;
  const secondStore = new SemanticIdempotencyStore(options);
  await secondStore.activateIdentity("user-a");
  const second = await secondStore.acquire(scope, payload);
  await secondStore.confirm(second);

  now = 1_400;
  const thirdStore = new SemanticIdempotencyStore(options);
  await thirdStore.activateIdentity("user-a");
  const third = await thirdStore.acquire(scope, payload);

  assert.deepEqual(
    [first.generation, second.generation, third.generation],
    [1, 2, 3],
  );
  assert.equal(new Set([first.key, second.key, third.key]).size, 3);
});

test("two stores converge through a shared cross-context lock and reload state", async () => {
  const storage = new MemoryStorage();
  const lockRequest = serializedLockRequest();
  const options = {
    storage,
    storageKey: "semantic-concurrent",
    digest,
    lockRequest,
  };
  const firstTab = new SemanticIdempotencyStore(options);
  const secondTab = new SemanticIdempotencyStore(options);
  await Promise.all([
    firstTab.activateIdentity("user-a"),
    secondTab.activateIdentity("user-a"),
  ]);
  const scope = { operation: "poster_style.generate" };
  const payload = { prompt: "same paid request", count: 1 };

  const [first, second] = await Promise.all([
    firstTab.acquire(scope, payload),
    secondTab.acquire(scope, payload),
  ]);

  assert.equal(first.key, second.key);
  assert.match(first.key, /^[a-f0-9]{64}$/);
  assert.equal(first.key.length, 64);
  assert.equal((await firstTab.acquire(scope, payload)).key, second.key);
  assert.equal(
    Object.keys(persistedRoot(storage, options.storageKey).pending).length,
    1,
  );
});

test("cross-tab locking allocates distinct monotonic sequences", async () => {
  const storage = new MemoryStorage();
  const lockRequest = serializedLockRequest();
  const options = {
    storage,
    storageKey: "semantic-concurrent-sequence",
    digest,
    lockRequest,
  };
  const firstTab = new SemanticIdempotencyStore(options);
  const secondTab = new SemanticIdempotencyStore(options);
  await Promise.all([
    firstTab.activateIdentity("user-a"),
    secondTab.activateIdentity("user-a"),
  ]);

  const [first, second] = await Promise.all([
    firstTab.acquire("scope", { id: "a" }),
    secondTab.acquire("scope", { id: "b" }),
  ]);

  assert.deepEqual(
    [first.generation, second.generation].sort((left, right) => left - right),
    [1, 2],
  );
  assert.notEqual(first.key, second.key);
  assert.equal(persistedRoot(storage, options.storageKey).sequence, 2);
});

test("Web Locks are preferred over IndexedDB coordination", async () => {
  const storage = new MemoryStorage();
  const calls: string[] = [];
  let indexedDbOpens = 0;
  const originalNavigator = Object.getOwnPropertyDescriptor(
    globalThis,
    "navigator",
  );
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: {
      locks: {
        async request<T>(
          name: string,
          _options: { mode: "exclusive" },
          callback: () => T | Promise<T>,
        ): Promise<T> {
          calls.push(name);
          return callback();
        },
      },
    },
  });
  try {
    const store = new SemanticIdempotencyStore({
      storage,
      storageKey: "semantic-locks",
      digest,
      indexedDb: {
        open() {
          indexedDbOpens += 1;
          throw new Error("IndexedDB must not be opened");
        },
      } as unknown as IDBFactory,
    });

    await store.activateIdentity("user-a");
    const lease = await store.acquire("scope", { id: "a" });
    await store.confirm(lease);

    assert.ok(calls.length >= 3);
    assert.ok(calls.every((name) => name === "semantic-locks.lock"));
    assert.equal(indexedDbOpens, 0);
  } finally {
    if (originalNavigator) {
      Object.defineProperty(globalThis, "navigator", originalNavigator);
    } else {
      Reflect.deleteProperty(globalThis, "navigator");
    }
  }
});

test("a fenced stale callback cannot write the semantic root", async () => {
  const storage = new MemoryStorage();
  const storageKey = "semantic-stale-fence-root";
  const deriveEntered = deferred<void>();
  const releaseDerivation = deferred<string>();
  let blockDerivation = false;
  let fenceCurrent = true;
  const store = new SemanticIdempotencyStore({
    storage,
    storageKey,
    freshNamespace: () => "fence-namespace",
    digest: async (value) => {
      if (blockDerivation && value.startsWith("idempotency-key:")) {
        deriveEntered.resolve(undefined);
        return releaseDerivation.promise;
      }
      return digest(value);
    },
    lockRequest: async (_name, _options, callback) =>
      callback({
        assertCurrent() {
          if (!fenceCurrent) {
            throw new SemanticIdempotencyDurabilityError(
              "storage lock ownership was lost",
            );
          }
        },
        runMutation(mutation) {
          if (!fenceCurrent) {
            throw new SemanticIdempotencyDurabilityError(
              "storage lock ownership was lost",
            );
          }
          return mutation();
        },
      }),
  });
  await store.activateIdentity("user-fenced");
  const before = storage.getItem(storageKey);
  blockDerivation = true;
  const acquisition = store.acquire(
    { operation: "fenced-write" },
    { prompt: "must not persist" },
  );
  await deriveEntered.promise;
  fenceCurrent = false;
  releaseDerivation.resolve(digest("released-key"));

  await assert.rejects(acquisition, /storage lock ownership was lost/);
  assert.equal(storage.getItem(storageKey), before);
});

test("a borrowed stale lease cannot discard the submitted creator lease", async () => {
  const storage = new MemoryStorage();
  const options = {
    storage,
    storageKey: "semantic-borrowed-discard",
    digest,
    lockRequest: null,
  };
  const firstTab = new SemanticIdempotencyStore(options);
  const secondTab = new SemanticIdempotencyStore(options);
  await Promise.all([
    firstTab.activateIdentity("user-a"),
    secondTab.activateIdentity("user-a"),
  ]);
  const scope = { operation: "conversation.message.create" };
  const payload = { text: "same paid send" };

  const creator = await firstTab.acquire(scope, payload);
  await firstTab.markSubmitted(creator);
  const borrowed = await secondTab.acquire(scope, payload);
  assert.equal(creator.ownership, "created");
  assert.equal(borrowed.ownership, "borrowed");
  assert.equal(borrowed.key, creator.key);

  await secondTab.discard(borrowed);
  await firstTab.recordFailure(creator, { status: 503 });
  const retry = await firstTab.acquire(scope, payload);
  const pending = Object.values(
    persistedRoot(storage, options.storageKey).pending,
  )[0] as {
    submitted?: boolean;
    shared?: boolean;
  };

  assert.equal(retry.key, creator.key);
  assert.equal(pending.submitted, true);
  assert.equal(pending.shared, true);
});

test("an unsubmitted exclusive creator discard advances to a new key", async () => {
  const storage = new MemoryStorage();
  const options = {
    storage,
    storageKey: "semantic-created-discard",
    digest,
    lockRequest: null,
  };
  const store = new SemanticIdempotencyStore(options);
  await store.activateIdentity("user-a");
  const scope = { operation: "conversation.message.create" };
  const payload = { text: "local abort" };

  const first = await store.acquire(scope, payload);
  await store.discard(first);
  const second = await store.acquire(scope, payload);

  assert.equal(first.ownership, "created");
  assert.notEqual(second.key, first.key);
  assert.equal(second.generation, first.generation + 1);
});

test("failed terminal-state writes fail closed and reload recovers the unresolved key", async () => {
  const storage = new MemoryStorage();
  const options = {
    storage,
    storageKey: "semantic-write-verification",
    digest,
    lockRequest: null,
  };
  const store = new SemanticIdempotencyStore(options);
  await store.activateIdentity("user-a");
  const scope = { operation: "video.generation.create" };
  const payload = { prompt: "durable retry" };
  const original = await store.acquire(scope, payload);
  await store.recordFailure(original, new TypeError("response lost"));

  storage.setWrites("drop");
  await assert.rejects(
    store.confirm(original),
    SemanticIdempotencyDurabilityError,
  );

  storage.setWrites("ok");
  const reloaded = new SemanticIdempotencyStore(options);
  await reloaded.activateIdentity("user-a");
  assert.equal((await reloaded.acquire(scope, payload)).key, original.key);
});

test("confirm treats a committed terminal root as success after post-callback fence loss", async () => {
  const storage = new MemoryStorage();
  const storageKey = "semantic-confirm-post-fence";
  let terminalRootCommitted = false;
  const lockRequest: ExclusiveLockRequest = async (
    _name,
    _options,
    callback,
  ) =>
    callback({
      assertCurrent() {
        if (terminalRootCommitted) {
          throw new SemanticIdempotencyDurabilityError(
            "indexeddb storage lock ownership was lost",
          );
        }
      },
      runMutation(mutation) {
        if (terminalRootCommitted) {
          throw new SemanticIdempotencyDurabilityError(
            "indexeddb storage lock ownership was lost",
          );
        }
        const beforeRaw = storage.getItem(storageKey);
        const beforePending =
          beforeRaw === null
            ? 0
            : Object.keys(
                (JSON.parse(beforeRaw) as { pending?: object }).pending ?? {},
              ).length;
        const result = mutation();
        const afterRaw = storage.getItem(storageKey);
        const afterPending =
          afterRaw === null
            ? 0
            : Object.keys(
                (JSON.parse(afterRaw) as { pending?: object }).pending ?? {},
              ).length;
        if (beforePending > 0 && afterPending === 0) {
          terminalRootCommitted = true;
        }
        return result;
      },
    });
  const store = new SemanticIdempotencyStore({
    storage,
    storageKey,
    digest,
    lockRequest,
  });
  const scope = { operation: "conversation.message.create" };
  const payload = { text: "server already succeeded" };
  await store.activateIdentity("user-a");
  const confirmed = await store.acquire(scope, payload);
  await store.markSubmitted(confirmed);

  await store.confirm(confirmed);
  terminalRootCommitted = false;
  const intentionalRetry = await store.acquire(scope, payload);

  assert.notEqual(intentionalRetry.key, confirmed.key);
  assert.equal(intentionalRetry.generation, confirmed.generation + 1);
});

test("authenticated browser storage getter failure fails closed before key generation", async () => {
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const originalStorage = Object.getOwnPropertyDescriptor(
    globalThis,
    "localStorage",
  );
  let generatedKeys = 0;
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {},
  });
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    get() {
      throw new Error("storage access blocked");
    },
  });
  try {
    const store = new SemanticIdempotencyStore({
      digest,
      freshKey: () => {
        generatedKeys += 1;
        return `unsafe-key-${generatedKeys}`;
      },
      lockRequest: null,
    });
    await store.activateIdentity("user-a");

    await assert.rejects(
      store.acquire("scope", { id: "paid" }),
      SemanticIdempotencyDurabilityError,
    );
    assert.equal(generatedKeys, 0);
  } finally {
    if (originalWindow) {
      Object.defineProperty(globalThis, "window", originalWindow);
    } else {
      Reflect.deleteProperty(globalThis, "window");
    }
    if (originalStorage) {
      Object.defineProperty(globalThis, "localStorage", originalStorage);
    } else {
      Reflect.deleteProperty(globalThis, "localStorage");
    }
  }
});

test("no Web Locks or IndexedDB fails closed without using localStorage as a lock", async () => {
  const originalNavigator = Object.getOwnPropertyDescriptor(
    globalThis,
    "navigator",
  );
  const values = new Map<string, string>();
  const storage = {
    getItem(key: string): string | null {
      return values.get(key) ?? null;
    },
    setItem(key: string, value: string): void {
      values.set(key, value);
    },
    removeItem(key: string): void {
      values.delete(key);
    },
  };
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: {},
  });
  try {
    const store = new SemanticIdempotencyStore({
      storage,
      storageKey: "semantic-nonenumerable-lock",
      digest,
      indexedDb: null,
    });
    await store.activateIdentity("user-a");

    await assert.rejects(
      store.acquire("scope", { id: "paid" }),
      SemanticIdempotencyDurabilityError,
    );
    assert.equal(values.size, 0);
  } finally {
    if (originalNavigator) {
      Object.defineProperty(globalThis, "navigator", originalNavigator);
    } else {
      Reflect.deleteProperty(globalThis, "navigator");
    }
  }
});

test("unavailable coordination fails before mutating quota-limited storage", async () => {
  const originalNavigator = Object.getOwnPropertyDescriptor(
    globalThis,
    "navigator",
  );
  const storage = new MemoryStorage(1);
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: {},
  });
  try {
    const store = new SemanticIdempotencyStore({
      storage,
      storageKey: "semantic-lock-quota",
      digest,
      indexedDb: null,
    });

    await store.activateIdentity("user-a");

    await assert.rejects(
      store.acquire("scope", { id: "paid" }),
      SemanticIdempotencyDurabilityError,
    );
    assert.equal(storage.length, 0);
  } finally {
    if (originalNavigator) {
      Object.defineProperty(globalThis, "navigator", originalNavigator);
    } else {
      Reflect.deleteProperty(globalThis, "navigator");
    }
  }
});

test("unresolved ambiguous entries survive TTL and capacity pressure", async () => {
  let now = 1_000;
  const storage = new MemoryStorage();
  const options = {
    ttlMs: 100,
    maxEntries: 2,
    now: () => now,
    storage,
    storageKey: "semantic-unresolved-retention",
    digest,
    lockRequest: null,
  };
  const store = new SemanticIdempotencyStore(options);
  await store.activateIdentity("user-a");
  const original = await store.acquire("scope", { id: "a" });
  await store.recordFailure(original, { status: 503 });
  for (const id of ["b", "c", "d"]) {
    const lease = await store.acquire("scope", { id });
    now += 1;
    await store.confirm(lease);
  }

  now = 10_000;
  const reloaded = new SemanticIdempotencyStore(options);
  await reloaded.activateIdentity("user-a");

  assert.equal((await reloaded.acquire("scope", { id: "a" })).key, original.key);
  assert.equal(
    Object.keys(persistedRoot(storage, options.storageKey).pending).length,
    1,
  );
  assert.equal(storage.keysMatching(".entry.").length, 2);
});

test("resolved tombstones stay quota-bounded and evicted intents never reuse keys", async () => {
  let now = 1_000;
  const storage = new MemoryStorage(4);
  const options = {
    maxEntries: 2,
    now: () => now,
    storage,
    storageKey: "semantic-bounded-resolved",
    digest,
    freshNamespace: () => "bounded-namespace",
    lockRequest: null,
  };
  const store = new SemanticIdempotencyStore(options);
  await store.activateIdentity("user-a");
  const keys = new Map<string, string>();

  for (const id of ["a", "b", "c"]) {
    const lease = await store.acquire("scope", { id });
    keys.set(id, lease.key);
    now += 1;
    await store.confirm(lease);
  }

  assert.equal(storage.length, 4);
  const resolvedKeys = storage.keysMatching(".entry.");
  const fingerprints = Object.fromEntries(
    ["a", "b", "c"].map((id) => [
      id,
      digest(`request:${semanticRequestFingerprint("scope", { id })}`),
    ]),
  );
  assert.equal(resolvedKeys.length, 2);
  assert.ok(!resolvedKeys.some((key) => key.endsWith(fingerprints.a)));
  assert.ok(resolvedKeys.some((key) => key.endsWith(fingerprints.b)));
  assert.ok(resolvedKeys.some((key) => key.endsWith(fingerprints.c)));
  assert.equal(persistedRoot(storage, options.storageKey).sequence, 3);

  const reloaded = new SemanticIdempotencyStore(options);
  await reloaded.activateIdentity("user-a");
  const repeated = await reloaded.acquire("scope", { id: "a" });

  assert.equal(repeated.generation, 4);
  assert.notEqual(repeated.key, keys.get("a"));
  assert.equal(storage.length, 3);
});

test("explicit storage clearing rotates the random namespace before sequence one", async () => {
  const storage = new MemoryStorage();
  let namespaceSequence = 0;
  const options = {
    storage,
    storageKey: "semantic-cleared-storage",
    digest,
    freshNamespace: () => `namespace-${++namespaceSequence}`,
    lockRequest: null,
  };
  const firstStore = new SemanticIdempotencyStore(options);
  await firstStore.activateIdentity("user-a");
  const first = await firstStore.acquire("scope", { id: "same" });
  await firstStore.confirm(first);
  const firstRoot = persistedRoot(storage, options.storageKey);

  storage.clear();
  const secondStore = new SemanticIdempotencyStore(options);
  await secondStore.activateIdentity("user-a");
  const second = await secondStore.acquire("scope", { id: "same" });
  const secondRoot = persistedRoot(storage, options.storageKey);

  assert.equal(first.generation, 1);
  assert.equal(second.generation, 1);
  assert.notEqual(secondRoot.namespace, firstRoot.namespace);
  assert.notEqual(second.key, first.key);
});

test("legacy durable entries migrate without expiring unresolved keys", async () => {
  const storage = new MemoryStorage();
  const storageKey = "semantic-legacy";
  const scope = { operation: "prompt_enhancement.stream" };
  const payload = { text: "legacy retry" };
  const fingerprint = digest(
    `request:${semanticRequestFingerprint(scope, payload)}`,
  );
  storage.setItem(
    storageKey,
    JSON.stringify({
      version: 1,
      identity: digest("identity:user-a"),
      entries: [
        {
          fingerprint,
          key: "legacy-key",
          expiresAt: 1,
        },
      ],
    }),
  );
  const store = new SemanticIdempotencyStore({
    now: () => 10_000,
    storage,
    storageKey,
    digest,
    lockRequest: null,
  });

  await store.activateIdentity("user-a");

  assert.equal((await store.acquire(scope, payload)).key, "legacy-key");
  assert.match(storage.getItem(storageKey) ?? "", /"version":4/);
});

test("version two pending entries migrate into the atomic root journal", async () => {
  const storage = new MemoryStorage();
  const storageKey = "semantic-v2";
  const identity = digest("identity:user-a");
  const fingerprint = digest(
    `request:${semanticRequestFingerprint("scope", { id: "legacy-v2" })}`,
  );
  storage.setItem(
    storageKey,
    JSON.stringify({
      version: 2,
      identity,
      identityGeneration: 1,
    }),
  );
  storage.setItem(
    `${storageKey}.entry.${identity}.1.${fingerprint}`,
    JSON.stringify({
      version: 2,
      state: "pending",
      fingerprint,
      key: "v2-pending-key",
      generation: 7,
      expiresAt: 1,
    }),
  );
  const store = new SemanticIdempotencyStore({
    storage,
    storageKey,
    digest,
    freshNamespace: () => "migrated-v2-namespace",
    lockRequest: null,
  });

  await store.activateIdentity("user-a");
  const migrated = await store.acquire("scope", { id: "legacy-v2" });

  assert.equal(migrated.key, "v2-pending-key");
  assert.equal(migrated.generation, 1);
  assert.equal(persistedRoot(storage, storageKey).version, 3);
  assert.equal(storage.keysMatching(`.entry.${identity}.1.`).length, 0);
});

test("version three single root migrates before another identity activates", async () => {
  const storage = new MemoryStorage();
  const storageKey = "semantic-v3-partition-migration";
  const identityA = digest("identity:user-a");
  const scope = { operation: "video.generation.create" };
  const payload = { prompt: "recover A after B" };
  const fingerprint = digest(
    `request:${semanticRequestFingerprint(scope, payload)}`,
  );
  storage.setItem(
    storageKey,
    JSON.stringify({
      version: 3,
      namespace: "legacy-v3-namespace",
      sequence: 1,
      pending: {
        [fingerprint]: {
          key: "legacy-v3-pending-key",
          generation: 1,
          expiresAt: 1,
          submitted: true,
        },
      },
      identity: identityA,
      identityGeneration: 1,
    }),
  );
  const store = new SemanticIdempotencyStore({
    storage,
    storageKey,
    digest,
    freshNamespace: keySequence(),
    lockRequest: null,
  });

  await store.activateIdentity("user-b");
  const userB = await store.acquire(scope, payload);
  assert.notEqual(userB.key, "legacy-v3-pending-key");
  assert.equal(
    (JSON.parse(String(storage.getItem(storageKey))) as { version: number })
      .version,
    4,
  );

  await store.activateIdentity("user-a");
  assert.equal(
    (await store.acquire(scope, payload)).key,
    "legacy-v3-pending-key",
  );
  assert.doesNotMatch(storage.serialized(), /user-a|user-b|recover A after B/);
});

for (const failurePoint of ["identity-root", "catalog"] as const) {
  test(`version three migration fails closed after ${failurePoint} write failure`, async () => {
    const storage = new MemoryStorage();
    const storageKey = `semantic-v3-crash-${failurePoint}`;
    const identity = digest("identity:user-a");
    const namespace = "legacy-crash-namespace";
    const scope = { operation: "poster_style.generate" };
    const payload = { prompt: "migration retry" };
    const fingerprint = digest(
      `request:${semanticRequestFingerprint(scope, payload)}`,
    );
    storage.setItem(
      storageKey,
      JSON.stringify({
        version: 3,
        namespace,
        sequence: 1,
        pending: {
          [fingerprint]: {
            key: "legacy-crash-key",
            generation: 1,
            expiresAt: 1,
            submitted: true,
          },
        },
        identity,
        identityGeneration: 1,
      }),
    );
    const rootKey = `${storageKey}.root.${namespace}.${identity}`;
    let failed = false;
    storage.interceptWrites((key, value) => {
      const catalogWrite =
        key === storageKey &&
        (JSON.parse(value) as { version?: number }).version === 4;
      if (
        !failed &&
        ((failurePoint === "identity-root" && key === rootKey) ||
          (failurePoint === "catalog" && catalogWrite))
      ) {
        failed = true;
        throw new Error(`injected ${failurePoint} write failure`);
      }
    });
    const options = {
      storage,
      storageKey,
      digest,
      freshNamespace: keySequence(),
      lockRequest: null,
    };
    const interrupted = new SemanticIdempotencyStore(options);
    await interrupted.activateIdentity("user-a");

    await assert.rejects(
      interrupted.acquire(scope, payload),
      SemanticIdempotencyDurabilityError,
    );
    assert.equal(
      (JSON.parse(String(storage.getItem(storageKey))) as { version: number })
        .version,
      3,
    );
    assert.equal(
      storage.getItem(rootKey) !== null,
      failurePoint === "catalog",
    );

    storage.interceptWrites(null);
    const recovered = new SemanticIdempotencyStore(options);
    await recovered.activateIdentity("user-a");
    assert.equal(
      (await recovered.acquire(scope, payload)).key,
      "legacy-crash-key",
    );
  });
}

test("durable keys survive reload without storing identity or request content", async () => {
  const storage = new MemoryStorage();
  const options = {
    storage,
    storageKey: "semantic-private",
    digest,
    lockRequest: null,
  };
  const first = new SemanticIdempotencyStore(options);
  await first.activateIdentity("user-private");
  const lease = await first.acquire(
    { operation: "conversation.message.create" },
    { text: "private prompt", target: "image-secret" },
  );
  await first.recordFailure(lease, new TypeError("response lost"));

  assert.doesNotMatch(storage.serialized(), /user-private/);
  assert.doesNotMatch(storage.serialized(), /private prompt|image-secret/);

  const reloaded = new SemanticIdempotencyStore(options);
  await reloaded.activateIdentity("user-private");
  const replay = await reloaded.acquire(
    { operation: "conversation.message.create" },
    { target: "image-secret", text: "private prompt" },
  );
  assert.equal(replay.key, lease.key);

  await reloaded.confirm(replay);
  assert.doesNotMatch(storage.serialized(), new RegExp(lease.key));
  assert.notEqual(
    (
      await reloaded.acquire(
        { operation: "conversation.message.create" },
        { text: "private prompt", target: "image-secret" },
      )
    ).key,
    lease.key,
  );
});

test("identity partitions retain unresolved keys across B, logout, and relogin", async () => {
  const storage = new MemoryStorage();
  const options = {
    storage,
    storageKey: "semantic-identity-clear",
    digest,
    lockRequest: null,
  };
  const store = new SemanticIdempotencyStore(options);
  const scope = { operation: "video.generation.create" };
  const payload = { prompt: "same request" };

  await store.activateIdentity("user-a");
  const userA = await store.acquire(scope, payload);
  await store.markSubmitted(userA);
  await store.recordFailure(userA, new TypeError("response lost"));

  await store.activateIdentity("user-b");
  const userB = await store.acquire(scope, payload);
  await store.markSubmitted(userB);
  await store.recordFailure(userB, new TypeError("response lost"));
  assert.notEqual(userB.key, userA.key);
  assert.doesNotMatch(
    JSON.stringify(persistedRoot(storage, options.storageKey, "user-b")),
    new RegExp(userA.key),
  );
  assert.match(
    JSON.stringify(persistedRoot(storage, options.storageKey, "user-a")),
    new RegExp(userA.key),
  );

  await store.activateIdentity(null);
  assert.match(storage.serialized(), new RegExp(userA.key));
  assert.match(storage.serialized(), new RegExp(userB.key));

  await store.activateIdentity("user-a");
  assert.equal((await store.acquire(scope, payload)).key, userA.key);
  await store.activateIdentity("user-b");
  assert.equal((await store.acquire(scope, payload)).key, userB.key);
  assert.doesNotMatch(storage.serialized(), /user-a|user-b|same request/);
});

test("late identity A completion and failure cannot mutate identity B entries", async () => {
  const storage = new MemoryStorage();
  const store = new SemanticIdempotencyStore({
    storage,
    storageKey: "semantic-identity-epoch",
    digest,
    lockRequest: null,
  });
  const scope = { operation: "video.generation.create" };
  const payload = { prompt: "same request" };

  await store.activateIdentity("user-a");
  const userALease = await store.acquire(scope, payload);
  await store.recordFailure(userALease, new TypeError("response lost"));

  await store.activateIdentity("user-b");
  const userBLease = await store.acquire(scope, payload);
  assert.notEqual(userBLease.identityEpoch, userALease.identityEpoch);

  await store.recordFailure(userALease, new TypeError("late failure"));
  await store.confirm(userALease);
  await store.discard(userALease);

  assert.equal((await store.acquire(scope, payload)).key, userBLease.key);
  assert.match(
    JSON.stringify(
      persistedRoot(storage, "semantic-identity-epoch", "user-a"),
    ),
    new RegExp(userALease.key),
  );
  assert.doesNotMatch(
    JSON.stringify(
      persistedRoot(storage, "semantic-identity-epoch", "user-b"),
    ),
    new RegExp(userALease.key),
  );
});

test("an acquire whose digest finishes after identity change fails closed", async () => {
  const storage = new MemoryStorage();
  const requestDigest = deferred<string>();
  let blockedRequestValue = "";
  const store = new SemanticIdempotencyStore({
    storage,
    storageKey: "semantic-acquire-epoch",
    digest: (value) => {
      if (value.startsWith("request:") && !blockedRequestValue) {
        blockedRequestValue = value;
        return requestDigest.promise;
      }
      return digest(value);
    },
    lockRequest: null,
  });

  await store.activateIdentity("user-a");
  const staleAcquire = store.acquire(
    { operation: "video.generation.create" },
    { prompt: "identity-bound request" },
  );
  assert.ok(blockedRequestValue);

  await store.activateIdentity("user-b");
  requestDigest.resolve(digest(blockedRequestValue));
  await assert.rejects(
    staleAcquire,
    /semantic idempotency identity changed/,
  );

  const userBLease = await store.acquire(
    { operation: "video.generation.create" },
    { prompt: "identity-bound request" },
  );
  assert.match(storage.serialized(), new RegExp(userBLease.key));
});

test("idempotent POST request keeps header and body keys identical", () => {
  const request = idempotentPostRequest({
    idempotency_key: "request-1",
    value: 42,
  });
  const headers = new Headers(request.headers);

  assert.equal(request.method, "POST");
  assert.equal(headers.get("Idempotency-Key"), "request-1");
  assert.deepEqual(JSON.parse(String(request.body)), {
    idempotency_key: "request-1",
    value: 42,
  });
  assert.throws(
    () =>
      idempotentPostRequest(
        { idempotency_key: "body-key" },
        { headers: { "Idempotency-Key": "header-key" } },
      ),
    /must match/,
  );

  const headerOnly = semanticPostRequest("request-2", {
    signal: AbortSignal.abort(),
  });
  assert.equal(headerOnly.method, "POST");
  assert.equal(
    new Headers(headerOnly.headers).get("Idempotency-Key"),
    "request-2",
  );
  assert.equal(headerOnly.body, undefined);

  const json = semanticJsonPostRequest({ value: 7 }, "request-3");
  assert.equal(
    new Headers(json.headers).get("Idempotency-Key"),
    "request-3",
  );
  assert.deepEqual(JSON.parse(String(json.body)), { value: 7 });
});
